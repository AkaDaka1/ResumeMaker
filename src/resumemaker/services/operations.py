"""Operation service.

A paid operation is started only after the bot script has obtained consents and the payment side
has succeeded. Starting one performs, in a single transaction:

    create operation -> reserve credits (ledger debit) -> enqueue job

If any step fails the savepoint is rolled back, so the user is never charged for a word that was
not queued. A repeated call with the same idempotency key returns the original operation instead
of charging again.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumemaker.db.models import Job, LedgerEntry, Operation
from resumemaker.domain.enums import (
    JobType,
    OperationStatus,
    OperationType,
    ResumeLanguage,
    ResumeMode,
)
from resumemaker.domain.errors import (
    IdempotencyConflict,
    InvalidStateTransition,
    NotFound,
)
from resumemaker.domain.pricing import operation_price
from resumemaker.domain.states import OPERATION_STATES
from resumemaker.services.jobs import JobService
from resumemaker.services.ledger import LedgerService

JOB_TYPE_FOR_OPERATION: dict[OperationType, JobType] = {
    OperationType.PREVIEW: JobType.RENDER_PREVIEW,
    OperationType.FINAL: JobType.RENDER_FINAL,
    OperationType.RENORMALIZATION: JobType.NORMALIZATION,
}

#: Which status an operation reaches when its work finishes successfully.
SUCCESS_STATUS_FOR_OPERATION: dict[OperationType, OperationStatus] = {
    OperationType.PREVIEW: OperationStatus.PREVIEW_READY,
    OperationType.FINAL: OperationStatus.READY,
    OperationType.RENORMALIZATION: OperationStatus.READY,
}


@dataclass(frozen=True)
class StartOperationRequest:
    user_id: uuid.UUID
    operation_type: OperationType
    resume_language: ResumeLanguage
    idempotency_key: str
    resume_mode: ResumeMode = ResumeMode.UNIVERSAL
    scenario_id: str | None = None
    scenario_version: int | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        if self.resume_mode is ResumeMode.VACANCY:
            if not self.scenario_id or self.scenario_version is None:
                raise ValueError("vacancy mode requires scenario_id and scenario_version")
        elif self.scenario_id is not None and self.scenario_version is None:
            raise ValueError("scenario_id requires scenario_version")

    def fingerprint(self) -> tuple[object, ...]:
        return (
            self.operation_type,
            self.resume_language,
            self.resume_mode,
            self.scenario_id,
            self.scenario_version,
        )


@dataclass(frozen=True)
class StartOperationResult:
    operation: Operation
    job: Job
    debit_entry: LedgerEntry | None
    created: bool


class OperationService:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.ledger = LedgerService(session)
        self.jobs = JobService(session)

    # ---------------------------------------------------------------- reads

    def get(self, operation_id: uuid.UUID) -> Operation:
        operation = self.session.get(Operation, operation_id)
        if operation is None:
            raise NotFound(f"operation {operation_id} does not exist")
        return operation

    # --------------------------------------------------------------- writes

    def start(self, request: StartOperationRequest) -> StartOperationResult:
        existing = self.session.scalar(
            select(Operation).where(Operation.idempotency_key == request.idempotency_key)
        )
        if existing is not None:
            return self._replay(existing, request)

        reservation = operation_price(request.operation_type)
        try:
            with self.session.begin_nested():
                operation = Operation(
                    user_id=request.user_id,
                    operation_type=request.operation_type,
                    status=OperationStatus.QUEUED,
                    resume_language=request.resume_language,
                    resume_mode=request.resume_mode,
                    scenario_id=request.scenario_id,
                    scenario_version=request.scenario_version,
                    reserved_credits=reservation,
                    idempotency_key=request.idempotency_key,
                    metadata_json=self._safe_metadata(request.metadata),
                )
                self.session.add(operation)
                self.session.flush()

                debit = self.ledger.debit_for_operation(
                    user_id=request.user_id,
                    operation_id=operation.id,
                    amount=reservation,
                    reason=f"{request.operation_type.value} generation",
                )
                operation.balance_debit_entry_id = debit.id

                job = self.jobs.enqueue(
                    operation_id=operation.id,
                    job_type=JOB_TYPE_FOR_OPERATION[request.operation_type],
                    payload={
                        "resume_language": request.resume_language.value,
                        "resume_mode": request.resume_mode.value,
                    },
                )
                self.session.flush()
        except Exception:
            self.session.expire_all()
            raise

        return StartOperationResult(operation=operation, job=job, debit_entry=debit, created=True)

    def mark_in_progress(self, operation_id: uuid.UUID) -> Operation:
        operation = self._get_for_update(operation_id)
        self._transition(operation, OperationStatus.IN_PROGRESS)
        operation.started_at = dt.datetime.now(dt.UTC)
        job = self.jobs.get_by_operation(operation_id)
        if job.status.value == "queued":
            self.jobs.claim(job.id)
        self.session.flush()
        return operation

    def complete(
        self,
        operation_id: uuid.UUID,
        *,
        actual_credits: int,
        provider_request_id: str | None = None,
    ) -> Operation:
        """Finish successfully and reconcile the reservation with the real cost."""

        if actual_credits < 0:
            raise ValueError("actual_credits must be non-negative")
        operation = self._get_for_update(operation_id)
        if operation.status is not OperationStatus.IN_PROGRESS:
            raise InvalidStateTransition(
                entity="operation", current=operation.status.value, target="complete"
            )

        target = SUCCESS_STATUS_FOR_OPERATION[operation.operation_type]
        self.ledger.adjust_operation(
            user_id=operation.user_id,
            operation_id=operation.id,
            reserved=operation.reserved_credits,
            actual=actual_credits,
        )
        self._transition(operation, target)
        operation.actual_credits = actual_credits
        operation.finished_at = dt.datetime.now(dt.UTC)
        self._finish_job(operation_id, provider_request_id=provider_request_id)
        self.session.flush()
        return operation

    def fail(self, operation_id: uuid.UUID, *, failure_code: str) -> Operation:
        """Terminal technical failure: return the whole reservation exactly once."""

        operation = self._get_for_update(operation_id)
        if operation.status is OperationStatus.FAILED:
            return operation
        self._transition(operation, OperationStatus.FAILED)
        self._refund_reservation(operation, reason=f"operation failed: {failure_code}")
        operation.failure_code = failure_code
        operation.finished_at = dt.datetime.now(dt.UTC)
        self._finish_job(operation_id, failure_code=failure_code)
        self.session.flush()
        return operation

    def cancel(self, operation_id: uuid.UUID) -> Operation:
        operation = self._get_for_update(operation_id)
        self._transition(operation, OperationStatus.CANCELLED)
        self._refund_reservation(operation, reason="operation cancelled")
        operation.finished_at = dt.datetime.now(dt.UTC)
        job = self.jobs.get_by_operation(operation_id)
        if job.status.value in {"queued", "in_progress"}:
            self.jobs.cancel(job.id)
        self.session.flush()
        return operation

    # -------------------------------------------------------------- internals

    def _replay(self, operation: Operation, request: StartOperationRequest) -> StartOperationResult:
        stored = (
            operation.operation_type,
            operation.resume_language,
            operation.resume_mode,
            operation.scenario_id,
            operation.scenario_version,
        )
        if stored != request.fingerprint():
            raise IdempotencyConflict(
                f"operation idempotency key {request.idempotency_key!r} "
                "was already used for a different request"
            )
        job = self.jobs.get_by_operation(operation.id)
        debit = (
            self.session.get(LedgerEntry, operation.balance_debit_entry_id)
            if operation.balance_debit_entry_id
            else None
        )
        return StartOperationResult(
            operation=operation, job=job, debit_entry=debit, created=False
        )

    def _refund_reservation(self, operation: Operation, *, reason: str) -> None:
        if operation.balance_debit_entry_id is None:
            return
        self.ledger.refund_operation(
            user_id=operation.user_id,
            operation_id=operation.id,
            amount=operation.reserved_credits,
            reason=reason,
        )

    def _finish_job(
        self,
        operation_id: uuid.UUID,
        *,
        provider_request_id: str | None = None,
        failure_code: str | None = None,
    ) -> None:
        job = self.jobs.get_by_operation(operation_id)
        if job.status.value in {"succeeded", "cancelled"}:
            return
        if failure_code is not None:
            self.jobs.fail(job.id, failure_code=failure_code)
        else:
            self.jobs.succeed(job.id, provider_request_id=provider_request_id)

    def _get_for_update(self, operation_id: uuid.UUID) -> Operation:
        operation = self.session.scalar(
            select(Operation).where(Operation.id == operation_id).with_for_update()
        )
        if operation is None:
            raise NotFound(f"operation {operation_id} does not exist")
        return operation

    @staticmethod
    def _transition(operation: Operation, target: OperationStatus) -> None:
        OPERATION_STATES.require(operation.status.value, target.value)
        operation.status = target

    @staticmethod
    def _safe_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
        """Keep only primitive values; the API must never leak an ORM object into JSON columns."""

        safe: dict[str, Any] = {}
        for key, value in metadata.items():
            if isinstance(value, (str, int, float, bool)) or value is None:
                safe[str(key)] = value
        return safe
