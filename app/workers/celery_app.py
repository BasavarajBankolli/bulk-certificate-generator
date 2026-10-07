from celery import Celery

from app.core.config import get_settings

PROCESS_JOB_TASK = "process_job"

# `include` makes the worker import the task module; the API only publishes by task name.
celery_app = Celery(
    "certificate_generator",
    broker=get_settings().redis_url,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    # Job progress and results live in PostgreSQL, so no Celery result backend is needed.
    task_ignore_result=True,
    # Acknowledge the message only after the task finishes: if a worker dies mid-job,
    # Redis redelivers it and another worker resumes the job.
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    # Take one job at a time so a long job doesn't hold other queued jobs hostage.
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    timezone="UTC",
)


def enqueue_process_job(job_id: str) -> None:
    """Publish a job to the queue. Only the job ID travels through Redis.

    The task is sent by name so the API process does not need to import worker code.
    """
    celery_app.send_task(PROCESS_JOB_TASK, args=[job_id])
