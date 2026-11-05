"""Job queue states, including the bounded retry policy."""

from __future__ import annotations

import uuid

from resumemaker.domain.enums import JobStatus, OperationType, ResumeLanguage
from resumemaker.services.jobs import JobService
from resumemaker.services.operations import OperationService, StartOperationRequest


def start_operation(uow, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    with uow() as session:
        result = OperationService(session).start(
            StartOperationRequest(
                user_id=user_id,
                operation_type=OperationType.PREVIEW,
                resume_language=ResumeLanguage.RU,
                idempotency_key=f"job-{uuid.uuid4()}",
            )
        )
        return result.operation.id, result.job.id


def test_claim_marks_progress_and_counts_attempt(uow, make_user) -> None:
    user_id = make_user(credits=100)
    _, job_id = start_operation(uow, user_id)

    with uow() as session:
        job = JobService(session).claim(job_id)
        assert job.status is JobStatus.IN_PROGRESS
        assert job.attempts == 1
        assert job.started_at is not None


def test_retry_returns_job_to_queue_until_attempts_run_out(uow, make_user) -> None:
    user_id = make_user(credits=100)
    _, job_id = start_operation(uow, user_id)

    with uow() as session:
        service = JobService(session)
        service.claim(job_id)
        retried = service.retry_or_fail(job_id, failure_code="llm_5xx")
        assert retried.status is JobStatus.QUEUED

    with uow() as session:
        service = JobService(session)
        service.claim(job_id)
        service.retry_or_fail(job_id, failure_code="llm_5xx")

    with uow() as session:
        service = JobService(session)
        service.claim(job_id)
        failed = service.retry_or_fail(job_id, failure_code="llm_5xx")
        assert failed.status is JobStatus.FAILED
        assert failed.failure_code == "llm_5xx"
        assert failed.finished_at is not None


def test_success_records_provider_request_id(uow, make_user) -> None:
    user_id = make_user(credits=100)
    _, job_id = start_operation(uow, user_id)

    with uow() as session:
        service = JobService(session)
        service.claim(job_id)
        job = service.succeed(job_id, provider_request_id="req-42")
        assert job.status is JobStatus.SUCCEEDED
        assert job.provider_request_id == "req-42"


def test_cancel_is_terminal(uow, make_user) -> None:
    user_id = make_user(credits=100)
    _, job_id = start_operation(uow, user_id)

    with uow() as session:
        job = JobService(session).cancel(job_id)
        assert job.status is JobStatus.CANCELLED
