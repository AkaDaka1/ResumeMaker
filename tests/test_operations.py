"""Operation lifecycle: atomic paid start, reconciliation, refunds and replays."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select

from resumemaker.db.models import Job, Operation
from resumemaker.domain.enums import (
    JobStatus,
    JobType,
    OperationStatus,
    OperationType,
    ResumeLanguage,
    ResumeMode,
)
from resumemaker.domain.errors import (
    IdempotencyConflict,
    InsufficientFunds,
    InvalidStateTransition,
)
from resumemaker.domain.pricing import operation_price
from resumemaker.services.ledger import LedgerService
from resumemaker.services.operations import (
    OperationService,
    StartOperationRequest,
)


def preview_request(user_id: uuid.UUID, **overrides: object) -> StartOperationRequest:
    defaults: dict[str, object] = {
        "user_id": user_id,
        "operation_type": OperationType.PREVIEW,
        "resume_language": ResumeLanguage.RU,
        "idempotency_key": f"op-{uuid.uuid4()}",
    }
    defaults.update(overrides)
    return StartOperationRequest(**defaults)  # type: ignore[arg-type]


def balance(uow, user_id: uuid.UUID) -> int:
    with uow() as session:
        return LedgerService(session).balance(user_id)


def test_start_reserves_price_and_enqueues_job(uow, make_user) -> None:
    user_id = make_user(credits=100)
    request = preview_request(user_id)

    with uow() as session:
        result = OperationService(session).start(request)
        assert result.created is True
        assert result.operation.status is OperationStatus.QUEUED
        assert result.operation.reserved_credits == operation_price(OperationType.PREVIEW)
        assert result.operation.actual_credits is None
        assert result.debit_entry is not None
        assert result.debit_entry.amount == -operation_price(OperationType.PREVIEW)
        assert result.job.status is JobStatus.QUEUED
        assert result.job.job_type is JobType.RENDER_PREVIEW
        assert result.job.operation_id == result.operation.id

    assert balance(uow, user_id) == 0


def test_repeated_start_returns_the_same_operation(uow, make_user) -> None:
    user_id = make_user(credits=100)
    request = preview_request(user_id)

    with uow() as session:
        first = OperationService(session).start(request)
    with uow() as session:
        second = OperationService(session).start(request)
        assert second.created is False
        assert second.operation.id == first.operation.id
        assert second.job.id == first.job.id

    with uow() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == 1
        assert session.scalar(select(func.count()).select_from(Job)) == 1

    assert balance(uow, user_id) == 0


def test_repeated_key_with_different_request_conflicts(uow, make_user) -> None:
    user_id = make_user(credits=100)
    request = preview_request(user_id)

    with uow() as session:
        OperationService(session).start(request)

    with pytest.raises(IdempotencyConflict):
        with uow() as session:
            OperationService(session).start(
                preview_request(user_id, idempotency_key=request.idempotency_key,
                                resume_language=ResumeLanguage.EN)
            )

    assert balance(uow, user_id) == 0


def test_start_without_funds_leaves_no_partial_state(uow, make_user) -> None:
    user_id = make_user(credits=50)

    with pytest.raises(InsufficientFunds):
        with uow() as session:
            OperationService(session).start(preview_request(user_id))

    with uow() as session:
        assert session.scalar(select(func.count()).select_from(Operation)) == 0
        assert session.scalar(select(func.count()).select_from(Job)) == 0
    assert balance(uow, user_id) == 50


def test_complete_charges_actual_cost_and_refunds_difference(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        operation = OperationService(session).start(preview_request(user_id)).operation
        operation_id = operation.id

    with uow() as session:
        service = OperationService(session)
        service.mark_in_progress(operation_id)
        finished = service.complete(operation_id, actual_credits=30, provider_request_id="req-1")
        assert finished.status is OperationStatus.PREVIEW_READY
        assert finished.actual_credits == 30
        assert finished.finished_at is not None

    with uow() as session:
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status is JobStatus.SUCCEEDED
        assert job.provider_request_id == "req-1"
        assert job.attempts == 1

    assert balance(uow, user_id) == 70


def test_failure_refunds_the_full_reservation(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        operation = OperationService(session).start(preview_request(user_id)).operation
        operation_id = operation.id

    with uow() as session:
        service = OperationService(session)
        service.mark_in_progress(operation_id)
        failed = service.fail(operation_id, failure_code="llm_timeout")
        assert failed.status is OperationStatus.FAILED
        assert failed.failure_code == "llm_timeout"

    with uow() as session:
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status is JobStatus.FAILED

    assert balance(uow, user_id) == 100


def test_failing_twice_refunds_once(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        operation_id = OperationService(session).start(preview_request(user_id)).operation.id

    with uow() as session:
        OperationService(session).fail(operation_id, failure_code="boom")
    with uow() as session:
        OperationService(session).fail(operation_id, failure_code="boom")

    assert balance(uow, user_id) == 100


def test_complete_requires_in_progress(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        operation_id = OperationService(session).start(preview_request(user_id)).operation.id

    with pytest.raises(InvalidStateTransition):
        with uow() as session:
            OperationService(session).complete(operation_id, actual_credits=10)


def test_cancel_refunds_and_cancels_job(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        operation_id = OperationService(session).start(preview_request(user_id)).operation.id

    with uow() as session:
        cancelled = OperationService(session).cancel(operation_id)
        assert cancelled.status is OperationStatus.CANCELLED

    with uow() as session:
        job = session.scalar(select(Job).where(Job.operation_id == operation_id))
        assert job is not None
        assert job.status is JobStatus.CANCELLED

    assert balance(uow, user_id) == 100


def test_final_operation_ends_ready(uow, make_user) -> None:
    user_id = make_user(credits=300)

    with uow() as session:
        operation_id = OperationService(session).start(
            preview_request(
                user_id,
                operation_type=OperationType.FINAL,
                resume_mode=ResumeMode.VACANCY,
                scenario_id="backend_developer",
                scenario_version=1,
            )
        ).operation.id

    with uow() as session:
        service = OperationService(session)
        service.mark_in_progress(operation_id)
        finished = service.complete(
            operation_id, actual_credits=operation_price(OperationType.FINAL)
        )
        assert finished.status is OperationStatus.READY


def test_vacancy_requires_scenario() -> None:
    with pytest.raises(ValueError):
        StartOperationRequest(
            user_id=uuid.uuid4(),
            operation_type=OperationType.FINAL,
            resume_language=ResumeLanguage.RU,
            idempotency_key="x",
            resume_mode=ResumeMode.VACANCY,
        )


def test_scenario_id_requires_version() -> None:
    with pytest.raises(ValueError):
        StartOperationRequest(
            user_id=uuid.uuid4(),
            operation_type=OperationType.FINAL,
            resume_language=ResumeLanguage.RU,
            idempotency_key="x",
            scenario_id="backend_developer",
        )


def test_metadata_is_reduced_to_primitive_values(uow, make_user) -> None:
    user_id = make_user(credits=100)

    with uow() as session:
        result = OperationService(session).start(
            preview_request(user_id, metadata={"source": "bot", "object": object()})
        )
        assert result.operation.metadata_json == {"source": "bot"}
