# Bulk Certificate Generator API

A FastAPI service that accepts a list of recipients, generates a PDF certificate for each
one **in the background**, tracks progress per job, isolates failures to individual
certificates, and lets clients list and download the results.

> Design document: [`docs/HLD.md`](docs/HLD.md)

---

## 1. Project overview

A client submits one request with up to 1,000 recipients. The API validates it, stores a
**job** plus one **certificate** row per recipient in PostgreSQL, puts the job ID on a
Redis queue and returns `201` immediately. A Celery worker picks the job up, renders each
certificate with ReportLab, writes the PDF to storage and records the outcome
certificate by certificate. If one certificate fails, the worker marks it `FAILED` and
carries on with the rest.

## 2. Features

- `POST /api/v1/jobs`: validated bulk submission that returns without waiting for PDFs
- Background generation with Celery + Redis; workers scale independently of the API
- Per-certificate failure isolation and a job status of `COMPLETED`, `PARTIALLY_COMPLETED` or `FAILED`
- Live progress (`successful`, `failed`, `pending`, `progress`) while a job runs
- Certificate listing with status filter and pagination; PDF download
- `Idempotency-Key` header to prevent duplicate jobs on client retries
- Crash-safe worker: an interrupted job resumes without regenerating finished certificates
- One professional A4 landscape template (name, course, event, date, verification ID)
- Storage behind a small interface (local disk today, S3-ready)
- Consistent `{"detail": ...}` errors; no stack traces reach clients
- Alembic migrations, Docker Compose (5 services, health checks, non-root containers)
- 92 pytest tests that need no Redis, Celery or PostgreSQL by default

## 3. Architecture

```
            ┌──────────────┐  POST /jobs   ┌──────────────────────────┐
  Client ──▶│  FastAPI API │──────────────▶│ PostgreSQL               │
            │  (uvicorn)   │◀──────────────│ generation_jobs          │
            └──────┬───────┘  GET status   │ certificates             │
                   │                       └────────────▲─────────────┘
                   │ send_task("process_job", job_id)   │ read recipients /
                   ▼                                    │ write statuses
            ┌──────────────┐   job_id only   ┌──────────┴───────────┐
            │    Redis     │────────────────▶│  Celery worker(s)    │
            │   (broker)   │                 │  render + store PDFs │
            └──────────────┘                 └──────────┬───────────┘
                                                        │ storage.save(key, pdf)
                                                        ▼
                                        ┌──────────────────────────────┐
  Client ◀── GET /certificates/{id}/download ──│ Shared volume (/app/storage) │
                                        └──────────────────────────────┘
```

**PostgreSQL is the source of truth; Redis only carries a job ID.** The worker loads
everything else from the database, so a lost or duplicated queue message can never lose
or corrupt recipient data.

| Layer | Responsibility | Files |
|---|---|---|
| Routes | HTTP only: parse, call a service, shape the response | `app/api/routes/` |
| Dependencies | Provide DB session, storage, queue, services (overridable in tests) | `app/api/deps.py` |
| Schemas | Request validation and response models (drive `/docs`) | `app/schemas/` |
| Services | Business logic | `app/services/` |
| Template | `render_certificate(data) -> bytes`, a pure function | `app/templates/certificate.py` |
| Worker | Thin Celery task: session, retries, give-up path | `app/workers/` |

## 4. Tech stack

| Technology | Why |
|---|---|
| Python 3.12, FastAPI | Typed request handling, automatic OpenAPI docs |
| PostgreSQL 16 + SQLAlchemy 2.0 (sync) + psycopg 3 | Celery is synchronous, so one DB style serves both API and worker |
| Pydantic v2, pydantic-settings | Validation; configuration from environment variables |
| Alembic | Schema migrations |
| Celery 5 + Redis 7 | Separate, scalable worker process with redelivery and retries |
| ReportLab | Pure-Python PDF drawing, no system libraries needed |
| pytest, pypdf, ruff | Tests (pypdf reads text back out of generated PDFs), lint/format |
| Docker Compose | One-command local environment |

## 5. Project structure

```
app/
├── main.py                      # app factory: routers + exception handlers
├── api/
│   ├── deps.py                  # get_storage, get_enqueuer, get_*_service
│   ├── errors.py                # exceptions → {"detail": ...} responses
│   ├── router.py
│   └── routes/                  # jobs.py, certificates.py, health.py
├── core/                        # config.py, logging.py, exceptions.py
├── db/                          # base.py, session.py, models.py
├── schemas/                     # job.py, certificate.py, common.py
├── services/
│   ├── job_service.py           # create (idempotent), status, listing
│   ├── generation_service.py    # worker logic + derive_final_status
│   ├── certificate_service.py   # metadata + download rules
│   └── storage.py               # StorageBackend protocol + LocalStorage
├── templates/certificate.py     # the ReportLab certificate template
└── workers/
    ├── celery_app.py            # Celery config + enqueue_process_job
    └── tasks.py                 # process_job task (thin wrapper)
alembic/                         # env.py + versions/ (initial migration)
tests/                           # 92 tests, see "Testing"
docs/HLD.md                      # approved high-level design
Dockerfile, docker-compose.yml, .env.example
requirements.txt, requirements-dev.txt, pyproject.toml (ruff + pytest config)
```

## 6. Setup

Prerequisites: Docker Desktop (for the quick start), or Python 3.12+, PostgreSQL and
Redis for running locally.

```bash
git clone <repo-url> bulk-certificate-generator
cd bulk-certificate-generator
cp .env.example .env        # then set a real POSTGRES_PASSWORD
```

## 7. Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `DATABASE_URL` | yes | - | SQLAlchemy URL, e.g. `postgresql+psycopg://user:pass@localhost:5432/certificates` |
| `REDIS_URL` | no | `redis://localhost:6379/0` | Celery broker |
| `STORAGE_DIR` | no | `storage` | Where PDFs are written (must be shared by API and worker) |
| `MAX_BATCH_SIZE` | no | `1000` | Maximum recipients per job |
| `LOG_LEVEL` | no | `INFO` | Python logging level |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | for Docker | - | Used by Compose to create the database and build `DATABASE_URL` |

There are no credentials in source code. `DATABASE_URL` has no default, and Compose
refuses to start if the `POSTGRES_*` values are missing. `.env` is git-ignored.

## 8. Running with Docker

```bash
docker compose up --build
```

| Service | What it does |
|---|---|
| `db` | PostgreSQL 16 (health-checked with `pg_isready`) |
| `redis` | Redis 7 broker (health-checked with `redis-cli ping`) |
| `migrate` | Runs `alembic upgrade head` once, then exits |
| `api` | Uvicorn on <http://localhost:8000> (health-checked via `/api/v1/health`) |
| `worker` | `celery worker --concurrency=2` |

`api` and `worker` start only after `migrate` completes successfully. Both mount the
`certificates_data` volume, because the worker writes the PDFs and the API serves them. All app
containers run as the non-root `appuser`.

- Swagger UI: <http://localhost:8000/docs>
- Scale workers: `docker compose up --scale worker=3`
- Stop and delete data: `docker compose down -v`

### Running locally (without Docker for the app)

```bash
python -m venv .venv
.venv/Scripts/activate              # Windows;  source .venv/bin/activate on macOS/Linux
pip install -r requirements-dev.txt
# Start PostgreSQL and Redis on localhost (your own installs, or e.g.
#   docker run -d -p 5432:5432 -e POSTGRES_USER=... -e POSTGRES_PASSWORD=... -e POSTGRES_DB=... postgres:16-alpine
#   docker run -d -p 6379:6379 redis:7-alpine)
# and make DATABASE_URL / REDIS_URL in .env point at them.
alembic upgrade head
uvicorn app.main:app --reload
celery -A app.workers.celery_app worker --loglevel=INFO            # macOS/Linux
celery -A app.workers.celery_app worker --loglevel=INFO --pool=solo  # Windows
```

> The Compose file deliberately does not publish the `db`/`redis` ports to the host, so
> they cannot clash with services already running on your machine.

## 9. Database migrations

```bash
alembic upgrade head                                  # apply
alembic downgrade base                                # roll back everything
alembic revision --autogenerate -m "describe change"  # after editing app/db/models.py
alembic check                                         # fails if models and migrations drift
```

`alembic/env.py` reads `DATABASE_URL` from the same settings as the app. In Docker the
`migrate` service runs `upgrade head` automatically.

### Schema

**`generation_jobs`**: `id` (UUID PK), `event_name`, `certificate_date`, `status`,
`total_count`, `success_count`, `failure_count`, `idempotency_key` (UNIQUE),
`request_hash`, `error_message`, `created_at`, `updated_at`, `started_at`, `completed_at`.
Checks: valid status, `total_count > 0`, non-negative counters. Index on `status`.

**`certificates`**: `id` (UUID PK, also printed on the PDF), `job_id` (FK →
`generation_jobs.id`, `ON DELETE CASCADE`), `recipient_name`, `recipient_email`,
`course`, `status`, `storage_key`, `error_message`, `created_at`, `updated_at`,
`completed_at`. Checks: valid status, and `SUCCESS` requires a `storage_key`.
Composite index `(job_id, status)`.

## 10. API documentation

Interactive docs: `/docs` (Swagger) and `/redoc`. All paths are under `/api/v1`.

| Method | Path | Success | Errors |
|---|---|---|---|
| `POST` | `/jobs` (optional `Idempotency-Key` header) | `201` new job · `200` idempotent replay | `409` key reused with a different body · `422` validation · `503` queue unavailable |
| `GET` | `/jobs/{job_id}` | `200` status + counters | `404` · `422` malformed UUID |
| `GET` | `/jobs/{job_id}/certificates?status=&limit=&offset=` | `200` paginated list (`limit` 1-100, default 50) | `404` · `422` |
| `GET` | `/certificates/{certificate_id}` | `200` metadata (incl. `error_message`, `download_url`) | `404` |
| `GET` | `/certificates/{certificate_id}/download` | `200` `application/pdf` | `404` not found · `409` pending or failed · `500` file missing |
| `GET` | `/health` | `200` `{"status":"ok","database":"ok"}` | `503` database down |

Every error body has the form `{"detail": "<message>"}`. For `422` responses, `detail` is
FastAPI's list of field errors.

### Validation rules

| Field | Rule |
|---|---|
| `event_name`, `course` | required, trimmed, 1–200 characters |
| `name` | required, trimmed, 1–200 characters |
| `email` | valid email address, stored lower-case |
| `certificate_date` | valid ISO date, at most 365 days in the future |
| `recipients` | 1 to `MAX_BATCH_SIZE` (default 1000) items |
| duplicates | the same email + course twice in one request is rejected |
| text | must be printable with the certificate's built-in fonts (Latin characters, see *Known limitations*) |

## 11. Example requests

```bash
# Create a job
curl -i -X POST http://localhost:8000/api/v1/jobs \
  -H "Content-Type: application/json" \
  -H "Idempotency-Key: workshop-2026-batch-1" \
  -d '{
        "event_name": "Python Workshop 2026",
        "certificate_date": "2026-10-07",
        "recipients": [
          {"name": "Alice", "email": "alice@example.com", "course": "Python Workshop"},
          {"name": "Bob",   "email": "bob@example.com",   "course": "Python Workshop"}
        ]
      }'

# Poll status
curl http://localhost:8000/api/v1/jobs/<job_id>

# List only failed certificates
curl "http://localhost:8000/api/v1/jobs/<job_id>/certificates?status=FAILED"

# Download a PDF
curl -o certificate.pdf http://localhost:8000/api/v1/certificates/<certificate_id>/download
```

## 12. Example responses

`POST /api/v1/jobs` → `201 Created` (`Location: /api/v1/jobs/<job_id>`)
```json
{"job_id": "0c6ddd60-c36c-4fc5-8391-be604906a054", "status": "QUEUED", "total": 2}
```

`GET /api/v1/jobs/{job_id}` → `200 OK`
```json
{
  "job_id": "0c6ddd60-c36c-4fc5-8391-be604906a054",
  "event_name": "Python Workshop 2026",
  "certificate_date": "2026-10-07",
  "status": "PARTIALLY_COMPLETED",
  "total": 10,
  "successful": 9,
  "failed": 1,
  "pending": 0,
  "progress": 100,
  "error_message": null,
  "created_at": "2026-10-07T10:58:14.101Z",
  "started_at": "2026-10-07T10:58:14.180Z",
  "completed_at": "2026-10-07T10:58:14.420Z"
}
```

`GET /api/v1/jobs/{job_id}/certificates?status=FAILED` → `200 OK`
```json
{
  "items": [
    {
      "certificate_id": "5b0f6c1e-7d2a-4a53-9c1e-2f7a1c3d9e10",
      "job_id": "0c6ddd60-c36c-4fc5-8391-be604906a054",
      "recipient_name": "Bob",
      "recipient_email": "bob@example.com",
      "course": "Python Workshop",
      "status": "FAILED",
      "error_message": "Unable to generate certificate",
      "download_url": null,
      "created_at": "2026-10-07T10:58:14.101Z",
      "completed_at": "2026-10-07T10:58:14.300Z"
    }
  ],
  "total": 1,
  "limit": 50,
  "offset": 0
}
```

Error examples:
```text
{"detail": "Job not found"}                                                     // 404
{"detail": "Certificate generation failed: Unable to generate certificate"}     // 409
{"detail": "Certificate is not ready yet (status: PENDING)"}                    // 409
{"detail": "Idempotency-Key has already been used with a different request"}    // 409
{"detail": "Job queue is unavailable, please retry later"}                      // 503
```

## 13. Background processing

**Why Celery + Redis rather than FastAPI `BackgroundTasks`:** `BackgroundTasks` runs inside
the API process. Work is lost on restart, it competes with request handling for CPU,
and it cannot be scaled or retried. Bulk generation is the core of this assignment, so
it runs in a separate worker process that can be scaled with `--scale worker=N`.

**Flow**

```
API                                          Worker: GenerationService.process_job(job_id)
───                                          ───────────────────────────────────────────
validate (Pydantic) → 422 if invalid         load job; if COMPLETED/PARTIAL/FAILED → return
idempotency check                            job → PROCESSING, started_at; commit
INSERT job + N certificates  (1 transaction) for each PENDING/PROCESSING certificate:
COMMIT                                           certificate → PROCESSING; commit
send_task("process_job", job_id)                 try: validate → render PDF → storage.save
return 201                                       ok:   SUCCESS, storage_key; success_count += 1
                                                 fail: FAILED, safe message; failure_count += 1
                                                 commit  ← one commit per certificate
                                             job → derive_final_status(); completed_at; commit
```

Key choices:
- **Enqueue after commit.** The worker can never receive a job ID that isn't saved yet.
- **One task per job.** One process owns a job, so deciding its final status needs no
  distributed coordination. Many jobs run in parallel across worker processes.
- **One commit per certificate.** Pollers see progress immediately, and a crash never
  rolls back earlier successes.
- **Counters updated in SQL in the same transaction as the certificate row**
  (`success_count = success_count + 1`), so they can never disagree with statuses.
- **Celery settings.** `task_acks_late` + `task_reject_on_worker_lost` mean a worker that
  dies mid-job leaves the message to be redelivered. `worker_prefetch_multiplier=1`
  stops one worker hoarding jobs. There is no result backend, because results live in
  PostgreSQL.
- **Retries.** A database `OperationalError` retries the task with backoff (5 s, 10 s,
  20 s). If it is still failing after 3 retries, the job is marked `FAILED`. Any other
  unexpected exception marks the job `FAILED` right away.

Measured in Docker on a laptop: 100 certificates in about 1.5 s, and 1,000 certificates
in about 11 s per job, with two such jobs processed in parallel by `--concurrency=2`.

### Status model

| Certificate | Meaning |
|---|---|
| `PENDING` | created, not started |
| `PROCESSING` | being rendered/stored right now |
| `SUCCESS` | PDF stored, `storage_key` set |
| `FAILED` | `error_message` set |

| Job | When |
|---|---|
| `QUEUED` | saved and queued; no worker has started it |
| `PROCESSING` | a worker started it (`started_at` set) |
| `COMPLETED` | every certificate succeeded |
| `PARTIALLY_COMPLETED` | at least one succeeded and at least one failed |
| `FAILED` | every certificate failed, **or** a job-level error (retries exhausted / unexpected crash). Unfinished certificates are then failed with `"Job processing was aborted"` and `error_message` explains why |

`pending = total − successful − failed`.
`progress = (successful + failed) × 100 ÷ total`. This is the percentage of certificates
*processed*, so a finished job always shows `100`, whether or not some certificates failed.

## 14. Failure handling

| Failure | Behaviour |
|---|---|
| Invalid request | `422` at the API; nothing is stored or queued |
| One certificate fails to render | That certificate → `FAILED` (`"Unable to generate certificate"`); the loop continues |
| Storage write fails for one certificate | That certificate → `FAILED` (`"Unable to store certificate"`); the loop continues |
| Bad recipient row reaches the worker | That certificate → `FAILED` (`"Invalid recipient data"`); the loop continues |
| Database briefly unavailable (worker) | The task retries with backoff. This is safe because finished certificates are skipped |
| Worker process dies mid-job | The message is redelivered; the job resumes from unfinished certificates |
| Redis down when creating a job | The just-created job is deleted and the API returns `503`, so the client can retry safely |
| Database down (API) | `503 {"detail": "Service temporarily unavailable"}` |
| Bug / unexpected exception (API) | `500 {"detail": "Internal server error"}`. The traceback is logged and never returned |

Certificates store only **client-safe** messages. The real exception and traceback are
logged with `job_id` and `certificate_id`.

## 15. Idempotency

**API.** `POST /jobs` accepts an optional `Idempotency-Key` header.

1. The service computes `request_hash = sha256(validated request JSON)`. Validation happens
   first, so `" Alice "` / `ALICE@x.com` hash the same as `Alice` / `alice@x.com`.
2. If no job has this key, it creates the job and stores the key and hash → `201`.
3. If a job has this key with the same hash, it returns that job → `200`. Nothing new
   is created or enqueued.
4. If a job has this key with a different hash, it returns `409`.
5. **Race:** if two identical requests arrive at once, the `UNIQUE` constraint lets only
   one `INSERT` win. The loser catches `IntegrityError`, rolls back, and returns the
   winner's job.

Without the header, every request creates a new job.

**Worker.** Running `process_job` twice is harmless. A finished job is skipped, only
`PENDING`/`PROCESSING` certificates are processed, and storage keys are deterministic
(`certificates/<job_id>/<certificate_id>.pdf`), so a regenerated file overwrites the old
one rather than creating a duplicate.

## 16. Testing

```bash
pip install -r requirements-dev.txt
pytest                         # 92 tests, in-memory SQLite, ~4 s
ruff check . && ruff format --check .

# Same suite against a real PostgreSQL database (it creates and drops the tables):
TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/certificates_test pytest
```

Tests use no external services. The database is in-memory SQLite (or PostgreSQL via
`TEST_DATABASE_URL`). The queue is a `FakeQueue` injected through
`app.dependency_overrides` that records job IDs. Storage is a `LocalStorage` in pytest's
`tmp_path`. Worker logic is tested by calling `GenerationService` directly with fake
renderers and storages; the Celery wrapper is tested by calling the task function.

| File | Covers |
|---|---|
| `test_jobs_api.py` | valid job, normalisation, empty list, invalid email, missing/blank name, oversized batch, duplicate recipient, unrenderable text, bad date, malformed JSON, queue down → 503 |
| `test_idempotency.py` | same key + same body, equivalent body, same key + different body (409), no key, different keys, concurrent duplicate |
| `test_certificate_template.py` | valid one-page A4-landscape PDF; name, course, event, date and ID in the text; long names; accented characters |
| `test_generation_service.py` | `derive_final_status`, all succeed, PDF content, all fail, storage failure, invalid row, rerun is a no-op, resume after crash, `mark_job_failed` |
| `test_failure_handling.py` | **10 recipients, 1 render failure → 9 PDFs on disk, 1 FAILED, job PARTIALLY_COMPLETED, API shows 9/1/0/100** |
| `test_job_status.py` | QUEUED, PROCESSING (observed live from inside the worker), COMPLETED, PARTIALLY_COMPLETED, FAILED, 404, 422 |
| `test_certificates_api.py` | download success, failed (409), pending (409), missing (404), file missing (500), metadata, listing, status filter, pagination, invalid params |
| `test_tasks.py` | DB error → retry with backoff; unexpected error → job FAILED |
| `test_storage.py`, `test_models_and_schemas.py`, `test_error_handling.py`, `test_health.py` | atomic writes, path traversal, constraints, 503/500 handling |

## 17. Design decisions

- **Thin routes, services for logic.** Routes never render PDFs, touch files or run SQL
  business rules. Services take their collaborators (session, storage, queue, renderer)
  as constructor arguments, which is what makes them easy to test.
- **Synchronous SQLAlchemy.** Celery workers are synchronous, and FastAPI runs `def`
  endpoints in a thread pool. One style everywhere is simpler than async plus sync.
- **`render_certificate` is a pure function.** Data in, bytes out: it is unit-tested
  without a database and is easy to swap in tests to simulate failures.
- **Storage keys, not paths.** The DB stores `certificates/<job>/<id>.pdf`.
  `LocalStorage` maps keys to files with atomic writes (temp file + `os.replace`) and
  refuses keys that escape the storage directory. An `S3Storage` with the same three
  methods could replace it by changing only `create_storage()`.
- **The API sends the task by name** (`send_task("process_job")`), so the API process
  never imports worker code.
- **Status as `VARCHAR` + `CHECK`** instead of a PostgreSQL `ENUM`, so adding a status
  later is a simple migration.
- **No separate `recipients` table.** Recipient and certificate are 1:1 within a job, so a
  second table would only add a join.
- **The status code lives on the domain exception** (`JobNotFoundError.status_code = 404`).
  One handler serves them all, and no separate mapping table can drift.

## 18. Trade-offs

| Decision | Cost accepted |
|---|---|
| Celery + Redis | One more container than `BackgroundTasks`, in exchange for a real, scalable, crash-tolerant worker |
| One task per job | A single huge job isn't split across workers (1,000 certificates ≈ 11 s, so acceptable here) |
| One commit per certificate | More DB round-trips, in exchange for live progress and crash-safe partial results |
| Stored counters | A small write cost, in exchange for O(1) status reads for polling clients |
| Compensating delete if enqueue fails | Avoids an outbox table; see the limitation on process crashes below |
| Idempotency key as a job column | Simple and correct for one endpoint; not a general idempotency store |
| PDF read into memory on download | Certificates are ~2–3 KB; streaming would add code for no benefit |
| SQLite for the default test run | Fast and dependency-free; the same suite also passes on PostgreSQL |

### Known limitations

- **Latin script only.** The built-in PDF fonts cover Windows-1252 characters
  (accents such as *José Müller* work). Names in other scripts (e.g. Chinese, Cyrillic) are
  rejected with `422` rather than producing a certificate with empty boxes.
- **Stuck jobs are not swept.** If the API process dies between committing a job and
  enqueuing it, the job stays `QUEUED`. If the database is still down when the worker
  gives up, the job stays `PROCESSING`. A periodic sweeper would fix both (see below).
- **Idempotency keys never expire.**
- **Local storage is single-host.** API and worker must share a volume.
- **No authentication or rate limiting.**

## 19. Future improvements

- Embed a Unicode TTF font (e.g. DejaVu Sans / Noto) to support all scripts
- Periodic sweeper that re-enqueues stuck `QUEUED`/`PROCESSING` jobs (or a transactional outbox)
- Split very large jobs into chunked tasks for parallelism within one job
- `S3Storage` + pre-signed download URLs; ZIP download of a whole job
- `POST /jobs/{id}/retry-failed` to regenerate only failed certificates
- Idempotency key TTL, authentication, rate limiting
- Structured JSON logs, Prometheus metrics (job duration, failure rate), email delivery

## 20. Learning from the assignment

- **Pick the unit of failure early.** Deciding that one *certificate*, not the job, can
  fail drove the design: a `try/except` per certificate, a commit per certificate,
  counters in the same transaction, and a derived final status.
- **Keep the queue dumb.** Putting only an ID on the queue and keeping state in
  PostgreSQL made retries, crashes and duplicate deliveries easy to reason about.
  Each one becomes "process whatever is still unfinished".
- **Idempotency has two sides.** Client retries need an API key and hash. Worker
  redelivery needs operations that are safe to repeat. They are solved differently.
- **Dependency injection pays off in tests.** Because the service receives its renderer,
  storage and queue, the most important test (9 of 10 succeed) is a few lines long, with
  no mocking library and no running Redis.
- **Look at the output.** Rendering the PDFs to images during development caught an
  underline drawn through the name and overlapping lines. Checking non-Latin names
  showed they would silently print as boxes, which led to explicit validation.
