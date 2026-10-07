# Bulk Certificate Generator — High-Level Design

Status: **Draft, awaiting approval**  ·  Date: 2026-10-07

---

## 1. Requirements analysis (Phase 1)

| Concern | What it really means for the design |
|---|---|
| Bulk + async | `POST /jobs` must return in milliseconds. Generation runs in a separate worker process, not in the request. |
| Failure isolation | The unit of failure is **one certificate**, not the job. Each recipient is processed in its own `try/except` and its own DB commit. |
| Progress tracking | Job counters must be readable cheaply while the worker is still running (clients poll). |
| Retrieval | Metadata and the PDF file are separate concerns: list/inspect via JSON, download via a file endpoint. |
| Idempotency | Two levels: (a) the **API** must not create duplicate jobs on client retries; (b) the **worker task** must be safe to run twice (crash/redelivery). |
| Replaceable storage | Business logic talks to a storage interface using *keys*, never absolute paths. |
| Explainability | Sync SQLAlchemy, one task type, no repositories/CQRS/event buses. |

**One ambiguity in the brief:** the example shows `successful: 96, failed: 4, pending: 0, progress: 96`. If nothing is pending, the job is 100% *processed*. I define **`progress` = % of certificates processed (success + failed) / total**, so that example would be `100`. This tells the client "is it done?", while `successful`/`failed` tell them "how did it go?".

---

## 2. Architecture

```
            ┌──────────────┐  POST /jobs   ┌──────────────────────────┐
  Client ──▶│  FastAPI API │──────────────▶│ PostgreSQL               │
            │  (uvicorn)   │◀──────────────│ generation_jobs          │
            └──────┬───────┘  GET status   │ certificates             │
                   │                       └────────────▲─────────────┘
                   │ enqueue(job_id)                    │ read recipients /
                   ▼                                    │ write statuses
            ┌──────────────┐   job_id only   ┌──────────┴───────────┐
            │    Redis     │────────────────▶│  Celery worker(s)    │
            │   (broker)   │                 │  generate PDFs       │
            └──────────────┘                 └──────────┬───────────┘
                                                        │ save(key, bytes)
                                                        ▼
                                        ┌──────────────────────────────┐
                                        │ Storage (local volume now,   │
  Client ◀── GET /certificates/{id}/download ── │ S3 later, same interface) │
                                        └──────────────────────────────┘
```

**Key principle: PostgreSQL is the source of truth; Redis only carries a job ID.**
The queue message is just `process_job(job_id)`. All recipient data lives in Postgres, so a lost/duplicated message never loses or corrupts data.

### Components

| Component | Responsibility |
|---|---|
| **API routes** | HTTP only: parse request, call service, map result to response/status code. No business logic. |
| **Schemas (Pydantic)** | Request validation and response shapes (drives `/docs`). |
| **JobService** | Create job + certificates in one transaction, idempotency check, enqueue, compute status view. |
| **GenerationService** | The worker's business logic: loop certificates, render, store, update status, finalize job. Plain Python — testable without Celery. |
| **Certificate template** | `render_certificate(data) -> bytes`. Pure function using ReportLab. No DB, no I/O. |
| **StorageService** | `StorageBackend` protocol + `LocalStorage` implementation. |
| **Celery task** | Thin wrapper: `process_job(job_id)` → `GenerationService.process_job(...)`. Retry config lives here. |
| **Exception handlers** | Map domain exceptions → consistent `{"detail": ...}` responses; log internals, never leak tracebacks. |

---

## 3. Background processing — choice and trade-offs

| Option | Pros | Cons | Verdict |
|---|---|---|---|
| FastAPI `BackgroundTasks` | Zero infra | Runs inside the API process: lost on restart/deploy, competes with request handling for CPU, can't scale workers independently, no retry | ❌ Not credible for *bulk* |
| **Celery + Redis** | Industry standard, separate scalable worker, `acks_late` redelivery, built-in retries, well-documented Docker setup | One extra container (Redis), some config surface | ✅ **Chosen** |
| RQ + Redis | Simpler API than Celery | Fewer reliability knobs, weaker Windows/dev story, less common in interviews | Good alternative |
| DB-polling worker (`SELECT … FOR UPDATE SKIP LOCKED`) | No Redis at all | Hand-rolled retries/visibility, more code to explain | Over-custom for this |

**Why Celery:** the assignment evaluates bulk processing, so the worker must be a real, independently scalable process (`docker compose up --scale worker=3`). Celery gives us redelivery after a worker crash and retry/backoff for transient DB errors with a few lines of config — things I'd otherwise have to hand-write.

### Task granularity: one task per job (not per certificate)

- `process_job(job_id)` loops over the job's certificates sequentially.
- **Why:** finalizing the job (deciding COMPLETED / PARTIAL / FAILED) is trivial when one process owns the job — no chords, no distributed counters, no race on "who is last".
- **Scaling:** many jobs process in parallel across workers. ReportLab renders a simple certificate in ~10–30 ms, so a max-size batch (1,000) finishes in well under a minute.
- **Trade-off:** a single huge job does not parallelize across workers. Future improvement: fan out per-chunk tasks (e.g. 100 certs each) with atomic counter updates. Documented, not built.

### Celery config (minimal, explainable)
- `task_acks_late=True`, `task_reject_on_worker_lost=True` → if a worker dies mid-job, the message is redelivered.
- `worker_prefetch_multiplier=1` → one long job doesn't hog queued jobs.
- `autoretry_for=(OperationalError,)`, `max_retries=3`, exponential backoff → transient DB outages.
- JSON serializer only.

---

## 4. Certificate generation flow

```
API                                          Worker (process_job)
───                                          ────────────────────
validate body (Pydantic)                     load job; if terminal → no-op (idempotent)
idempotency check                            job.status = PROCESSING, started_at = now; commit
BEGIN                                        for cert in certs WHERE status IN (PENDING, PROCESSING):
  INSERT job (QUEUED, total=N)                   cert.status = PROCESSING; commit
  INSERT N certificates (PENDING)                try:
COMMIT                                               validate recipient (defensive)
enqueue process_job(job_id)                          pdf = render_certificate(...)
return 201 {job_id, QUEUED, total}                   storage.save(key, pdf)
                                                     cert → SUCCESS, storage_key, completed_at
                                                     job.success_count += 1        ┐ same
                                                 except Exception:                    │ transaction
                                                     log traceback (job_id, cert_id)  │
                                                     cert → FAILED, safe error_message│
                                                     job.failure_count += 1        ┘
                                                 commit   ← per certificate
                                             job.status = derive_final_status(); completed_at; commit
```

- **Per-certificate commit**: progress is visible to pollers immediately, and a later crash never rolls back earlier successes.
- **Counters updated in the same transaction as the certificate row** (`UPDATE … SET success_count = success_count + 1`, done in SQL) so they can never drift from certificate statuses.
- **Deterministic storage key** `certificates/<job_id>/<certificate_id>.pdf` → regenerating after a crash simply overwrites the same file.
- **Defensive re-validation in the worker**: the API already rejects malformed input; the worker re-checks (e.g. empty name after normalization) so one bad row can't crash the loop. Rendering handles long names by shrinking the font to fit.

---

## 5. Status model

### Certificate status
| Status | Meaning |
|---|---|
| `PENDING` | Created, not yet picked up |
| `PROCESSING` | Worker is rendering/storing it right now |
| `SUCCESS` | PDF stored; `storage_key` is set |
| `FAILED` | Generation or storage failed; `error_message` is set |

### Job status
| Status | When |
|---|---|
| `QUEUED` | Job + certificates committed, task enqueued, no worker has started it |
| `PROCESSING` | A worker has started (`started_at` set); some certificates may still be pending |
| `COMPLETED` | All certificates processed, **every one** `SUCCESS` |
| `PARTIALLY_COMPLETED` | All processed, **≥1 SUCCESS and ≥1 FAILED** |
| `FAILED` | All processed and **every one** `FAILED`, **or** an unrecoverable job-level error after retries are exhausted (remaining certificates are then marked FAILED with reason) |

```
QUEUED ──▶ PROCESSING ──┬──▶ COMPLETED            (failed == 0)
                        ├──▶ PARTIALLY_COMPLETED  (success > 0 and failed > 0)
                        └──▶ FAILED               (success == 0)
```
Terminal statuses: `COMPLETED`, `PARTIALLY_COMPLETED`, `FAILED`. The final-status decision is a small pure function `derive_final_status(success, failed)` — unit-tested directly.

Status response fields: `pending = total − successful − failed` (includes in-flight), `progress = round((successful + failed) / total × 100)`.

---

## 6. Database schema

Improvements over the suggested schema are marked ★.

### `generation_jobs`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | generated in app (`uuid4`) |
| `event_name` | VARCHAR(200) NOT NULL | |
| `certificate_date` | DATE NOT NULL | |
| `status` | VARCHAR(30) NOT NULL | ★ CHECK constraint instead of a PG `ENUM` (adding a value later doesn't need `ALTER TYPE`) |
| `total_count` | INT NOT NULL | CHECK `> 0` |
| `success_count` | INT NOT NULL DEFAULT 0 | |
| `failure_count` | INT NOT NULL DEFAULT 0 | |
| `idempotency_key` | VARCHAR(255) NULL, **UNIQUE** | ★ |
| `request_hash` | CHAR(64) NULL | ★ SHA-256 of canonical request body |
| `error_message` | TEXT NULL | ★ job-level failure reason |
| `created_at` | TIMESTAMPTZ NOT NULL DEFAULT now() | |
| `started_at` / `completed_at` | TIMESTAMPTZ NULL | |
| `updated_at` | TIMESTAMPTZ NOT NULL | ★ helps detect stuck jobs |

Indexes: PK, unique(`idempotency_key`), index(`status`) for operational queries ("stuck PROCESSING jobs").

### `certificates`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | also printed on the PDF as a verification ID ★ |
| `job_id` | UUID FK → `generation_jobs.id` ON DELETE CASCADE, NOT NULL | |
| `recipient_name` | VARCHAR(200) NOT NULL | |
| `recipient_email` | VARCHAR(320) NOT NULL | 320 = RFC max |
| `course` | VARCHAR(200) NOT NULL | |
| `status` | VARCHAR(20) NOT NULL | CHECK constraint |
| `storage_key` | VARCHAR(500) NULL | ★ storage *key*, not filesystem path — S3-ready |
| `error_message` | TEXT NULL | client-safe message only; full traceback goes to logs |
| `created_at` / `updated_at` | TIMESTAMPTZ | |
| `completed_at` | TIMESTAMPTZ NULL | |

Indexes: ★ composite `(job_id, status)` — serves "list certificates of a job", "filter by status", and "fetch PENDING certs for the worker" with one index.

**Design note — no separate `recipients` table.** Recipient ↔ certificate is strictly 1:1 within a job, so splitting them adds a join with no benefit. If recipients became reusable entities across jobs, that would justify a table.

Migrations: Alembic, one initial revision; `alembic upgrade head` runs as a one-shot container before API/worker start.

---

## 7. API design

Base path `/api/v1`. All errors: `{"detail": "<message>"}` (422 keeps FastAPI's list-of-field-errors under `detail`).

| Method | Path | Purpose | Success | Errors |
|---|---|---|---|---|
| POST | `/jobs` | Create job (optional `Idempotency-Key` header) | **201** new / **200** idempotent replay | 409 key reused with different body, 422 validation, 503 queue unavailable |
| GET | `/jobs/{job_id}` | Job status + counters | 200 | 404, 422 bad UUID |
| GET | `/jobs/{job_id}/certificates?status=&limit=&offset=` | Paginated certificate list | 200 | 404 |
| GET | `/certificates/{certificate_id}` | Certificate metadata (status, error, download URL) | 200 | 404 |
| GET | `/certificates/{certificate_id}/download` | Stream the PDF (`application/pdf`) | 200 | 404 not found; **409** not ready (PENDING/PROCESSING) or FAILED (with reason) |
| GET | `/health` | Liveness + DB check | 200 | 503 |

Why split metadata and download: JSON clients can inspect a certificate (including *why* it failed) without downloading bytes, and the download endpoint has one clear job.

### Examples

`POST /api/v1/jobs` · `Idempotency-Key: 7f1c…`
```json
{
  "event_name": "Python Workshop 2026",
  "certificate_date": "2026-10-07",
  "recipients": [
    {"name": "Alice", "email": "alice@example.com", "course": "Python Workshop"},
    {"name": "Bob",   "email": "bob@example.com",   "course": "Python Workshop"}
  ]
}
```
→ `201 Created`, `Location: /api/v1/jobs/<id>`
```json
{"job_id": "3f6c…", "status": "QUEUED", "total": 2}
```

`GET /api/v1/jobs/3f6c…` → `200`
```json
{
  "job_id": "3f6c…", "event_name": "Python Workshop 2026", "certificate_date": "2026-10-07",
  "status": "PARTIALLY_COMPLETED",
  "total": 100, "successful": 96, "failed": 4, "pending": 0, "progress": 100,
  "created_at": "…", "started_at": "…", "completed_at": "…"
}
```

`GET /api/v1/jobs/3f6c…/certificates?status=FAILED` → `200`
```json
{
  "items": [
    {"certificate_id": "a1…", "recipient_name": "Bob", "recipient_email": "bob@example.com",
     "course": "Python Workshop", "status": "FAILED",
     "error_message": "Unable to generate certificate", "download_url": null}
  ],
  "total": 4, "limit": 50, "offset": 0
}
```

`GET /api/v1/certificates/a1…/download` (failed) → `409`
```json
{"detail": "Certificate generation failed: Unable to generate certificate"}
```

---

## 8. Validation (Pydantic, at the API boundary)

| Field | Rule |
|---|---|
| `event_name`, `course` | stripped, 1–200 chars |
| `name` | required, stripped, 1–200 chars |
| `email` | `EmailStr`, normalized to lower-case |
| `certificate_date` | valid ISO date (Pydantic `date`); reject dates unreasonably far in future (> 1 year) |
| `recipients` | 1 ≤ len ≤ `MAX_BATCH_SIZE` (env, default 1000) |
| duplicates | reject the same (email, course) twice in one request → 422 |

Malformed requests get **422** and never reach the DB or the queue.

---

## 9. Failure handling

| Failure | Where | Behaviour |
|---|---|---|
| Invalid request / recipient | API | 422, nothing persisted |
| Render error (one recipient) | Worker | That cert → FAILED, `"Unable to generate certificate"`; loop continues |
| Storage write error (one cert) | Worker | That cert → FAILED, `"Unable to store certificate"`; loop continues |
| Transient DB error | Worker | Celery retries the task (backoff, max 3). Safe because the task skips already-finished certs |
| Retries exhausted / unexpected crash | Worker | Job → FAILED with `error_message`; unfinished certs → FAILED |
| Worker process dies mid-job | Celery | `acks_late` → redelivered; resumes from PENDING/PROCESSING certs |
| Redis down at enqueue | API | Compensating delete of the just-created job, return **503**; client retries safely (same idempotency key creates it fresh) |
| DB down at request time | API | Generic **503** `{"detail": "Service temporarily unavailable"}`; full error logged |
| Unknown exception | API | Global handler → **500** `{"detail": "Internal server error"}`; traceback logged, never returned |

Error messages stored on certificates are **client-safe categories**; the raw exception + traceback is logged with `job_id`/`certificate_id` for debugging.

---

## 10. Idempotency

**API level — `Idempotency-Key` header (optional):**
1. Compute `request_hash = sha256(canonical JSON of validated body)`.
2. Look up job by `idempotency_key`.
   - Not found → create job, store key + hash → **201**.
   - Found, **same hash** → return the existing job → **200** (no new job, no new enqueue).
   - Found, **different hash** → **409** "Idempotency-Key already used with a different request".
3. **Race** (two identical requests at the same instant): the UNIQUE constraint makes the second INSERT fail → catch `IntegrityError`, roll back, re-read, and apply step 2.

No header → no deduplication (client opted out). Keys don't expire in this version (future: TTL cleanup). Stored as a column on the job rather than a separate table — simplest correct option because the key maps 1:1 to a job.

**Worker level — task is safe to run twice:**
- Job already terminal → no-op.
- Only certificates in PENDING/PROCESSING are processed.
- Deterministic storage keys → re-rendering overwrites, never duplicates.

---

## 11. Storage

```python
class StorageBackend(Protocol):
    def save(self, key: str, data: bytes) -> None: ...
    def open(self, key: str) -> BinaryIO: ...     # or path for FileResponse
    def exists(self, key: str) -> bool: ...
```
- `LocalStorage(base_dir)` writes to `STORAGE_DIR/certificates/<job_id>/<cert_id>.pdf` (atomic: write temp file then `os.replace`).
- Provided via FastAPI dependency / factory from settings → swapping to `S3Storage` changes one factory function; services are untouched.
- In Docker, API and worker share a named volume.
- Path-traversal safe: keys are built only from UUIDs, never user input.

---

## 12. Certificate template

Single ReportLab template, A4 landscape:
- Double decorative border, title **"Certificate of Completion"**
- "This is to certify that" → **recipient name** (large, auto-shrinks to fit width)
- "has successfully completed" → **course**, "at **event_name**"
- Date (formatted `07 October 2026`), signature line
- Footer: `Certificate ID: <uuid>` (verification reference)

Pure function `render_certificate(CertificateData) -> bytes` → unit-testable; tests extract text with `pypdf` to assert recipient details appear. Built-in fonts (Helvetica/Times) — no font files to ship.

---

## 13. Testing strategy

- **Framework:** pytest + FastAPI `TestClient`.
- **No external services:** tests use **SQLite in-memory** (SQLAlchemy `Uuid`/`DateTime` types are portable) and a **fake queue** (enqueue function injected via dependency, so tests capture `job_id` instead of hitting Redis). Worker logic is tested by calling `GenerationService.process_job()` directly — Celery is a thin wrapper.
- **Storage:** `LocalStorage` pointed at pytest's `tmp_path`.
- Optional: `TEST_DATABASE_URL` to run the same suite against real Postgres (e.g. in CI).

| File | Covers |
|---|---|
| `test_jobs.py` | valid create (201), empty recipients, invalid email, missing name, oversized batch, bad date, duplicate recipients → 422; status 404; status QUEUED |
| `test_generation.py` | render returns valid PDF bytes; file exists in storage; name/course/date appear in extracted text; long name renders |
| `test_job_status.py` | QUEUED → PROCESSING → COMPLETED / PARTIALLY_COMPLETED / FAILED; `derive_final_status` table test; progress math |
| `test_failure_handling.py` | **10 recipients, renderer mocked to fail for 1 → 9 SUCCESS files on disk, 1 FAILED with message, job PARTIALLY_COMPLETED**; all fail → FAILED; storage failure isolated; re-running task is a no-op / resumes |
| `test_certificates.py` | download success (PDF content-type), missing → 404, failed → 409, pending → 409, list + status filter + pagination |
| `test_idempotency.py` | same key + same body → same job, 1 row in DB; same key + different body → 409; no key → two jobs |

---

## 14. Project structure

```
bulk-certificate-generator/
├── app/
│   ├── main.py                    # app factory, routers, exception handlers
│   ├── api/
│   │   ├── deps.py                # get_db, get_storage, get_enqueuer
│   │   ├── errors.py              # domain exception → HTTP mapping
│   │   └── routes/
│   │       ├── jobs.py
│   │       ├── certificates.py
│   │       └── health.py
│   ├── core/
│   │   ├── config.py              # pydantic-settings (env vars)
│   │   ├── logging.py
│   │   └── exceptions.py          # JobNotFound, CertificateNotReady, IdempotencyConflict…
│   ├── db/
│   │   ├── base.py                # DeclarativeBase
│   │   ├── session.py             # engine, SessionLocal
│   │   └── models.py              # GenerationJob, Certificate, status enums
│   ├── schemas/
│   │   ├── job.py
│   │   └── certificate.py
│   ├── services/
│   │   ├── job_service.py         # create (idempotent), get status, list certs
│   │   ├── generation_service.py  # worker business logic + derive_final_status
│   │   ├── certificate_service.py # retrieval / download rules
│   │   └── storage.py             # StorageBackend protocol + LocalStorage
│   ├── templates/
│   │   └── certificate.py         # render_certificate() with ReportLab
│   └── workers/
│       ├── celery_app.py          # Celery instance + config
│       └── tasks.py               # process_job task (thin wrapper)
├── alembic/  + alembic.ini
├── tests/
│   ├── conftest.py
│   └── test_*.py
├── docs/HLD.md
├── Dockerfile
├── docker-compose.yml
├── .env.example
├── .dockerignore / .gitignore
├── requirements.txt / requirements-dev.txt
└── README.md
```
Changes from the suggested structure: `utils/` dropped (nothing generic enough to need it); `generation_service.py` added so worker logic is separate from API job logic; `celery_app.py` split from `tasks.py`; `core/exceptions.py` + `api/errors.py` for consistent error handling.

---

## 15. Docker setup

`docker compose up --build` starts:

| Service | Image | Notes |
|---|---|---|
| `db` | `postgres:16-alpine` | healthcheck `pg_isready`, named volume |
| `redis` | `redis:7-alpine` | healthcheck `redis-cli ping` |
| `migrate` | app image | `alembic upgrade head`, runs once; depends on `db` healthy |
| `api` | app image | `uvicorn app.main:app`, port 8000; depends on `migrate` completed |
| `worker` | app image | `celery -A app.workers.celery_app worker --concurrency=2`; depends on `migrate`, `redis` |

- One `Dockerfile` (python:3.12-slim, non-root user) used by api/migrate/worker.
- `certificates_data` named volume shared by `api` and `worker`.
- All config via env (`.env`, from `.env.example`): `DATABASE_URL`, `REDIS_URL`, `STORAGE_DIR`, `MAX_BATCH_SIZE`, `LOG_LEVEL`, `POSTGRES_USER/PASSWORD/DB`. No credentials hard-coded.

---

## 16. Technology choices

| Tech | Why |
|---|---|
| Python 3.12, FastAPI | required; auto OpenAPI docs |
| **Sync** SQLAlchemy 2.0 + psycopg 3 | Celery is sync anyway; one DB style across API and worker; FastAPI runs sync endpoints in a threadpool. Async would add complexity with no benefit at this scale |
| Pydantic v2 + pydantic-settings | validation + env config |
| Alembic | migrations |
| Celery + Redis | see §3 |
| ReportLab | mature, pure-Python PDF drawing, no system deps (vs WeasyPrint needing Cairo/Pango) |
| pytest, httpx, pypdf | tests; pypdf to assert text inside generated PDFs |

---

## 17. Key design decisions & trade-offs

| Decision | Trade-off accepted |
|---|---|
| Celery + Redis over BackgroundTasks | +1 container, in exchange for a real, scalable, crash-tolerant worker |
| One task per job | Simple finalization; a single huge job doesn't parallelize (future: chunked fan-out) |
| Per-certificate commits | More DB round-trips; gives live progress and crash-safe partial results |
| Stored counters (updated atomically with cert row) | Small write cost; O(1) status reads for polling clients |
| Postgres = source of truth, queue carries only `job_id` | Worker does one extra read; no data loss if Redis is flushed |
| Idempotency key as a job column | Can't be reused for other endpoints; simplest correct option here |
| Compensating delete if enqueue fails | Tiny window where a job exists un-enqueued; avoids an outbox table. Future: outbox or a periodic "re-enqueue stuck QUEUED jobs" sweeper |
| VARCHAR + CHECK instead of PG ENUM | Slightly weaker typing; painless migrations |
| SQLite for tests | Fast, no external deps; small dialect risk mitigated by optional Postgres test run |
| Local FS storage behind interface | Not multi-host; swap to S3 later without touching services |

### Future improvements (README)
Chunked parallel fan-out for very large jobs · S3 storage + pre-signed URLs · ZIP download of a whole job · retry-failed-certificates endpoint · stuck-job sweeper · idempotency key TTL · auth/rate limiting · email delivery · metrics (Prometheus) and structured JSON logs.
