"""Job service: explicit queue states for one paid operation.

Each operation owns exactly one job (`uq_jobs_operation_id`). Claiming locks the row, so a second
worker cannot pick up the same job.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumemaker.db.models import Job
from resumemaker.domain.enums import JobStatus, JobType
from resumemaker.domain.errors import InvalidStateTransition, NotFound
from resumemaker.domain.states import JOB_STATES


class JobService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def get(self, job_id: uuid.UUID, *, for_update: bool = False) -> Job:
        statement = select(Job).where(Job.id == job_id)
        if for_update:
            statement = statement.with_for_update()
        job = self.session.scalar(statement)
        if job is None:
            raise NotFound(f"job {job_id} does not exist")
        return job

    def get_by_operation(self, operation_id: uuid.UUID) -> Job:
        job = self.session.scalar(select(Job).where(Job.operation_id == operation_id))
        if job is None:
            raise NotFound(f"job for operation {operation_id} does not exist")
        return job

    def enqueue(
        self,
        *,
        operation_id: uuid.UUID,
        job_type: JobType,
        payload: dict[str, object] | None = None,
        max_attempts: int = 3,
    ) -> Job:
        job = Job(
            operation_id=operation_id,
            job_type=job_type,
            status=JobStatus.QUEUED,
            payload=payload,
            max_attempts=max_attempts,
        )
        self.session.add(job)
        self.session.flush()
        return job

    def claim(self, job_id: uuid.UUID) -> Job:
        job = self.get(job_id, for_update=True)
        self._transition(job, JobStatus.IN_PROGRESS)
        job.attempts += 1
        job.started_at = dt.datetime.now(dt.UTC)
        self.session.flush()
        return job

    def succeed(self, job_id: uuid.UUID, *, provider_request_id: str | None = None) -> Job:
        job = self.get(job_id)
        self._transition(job, JobStatus.SUCCEEDED)
        job.finished_at = dt.datetime.now(dt.UTC)
        if provider_request_id:
            job.provider_request_id = provider_request_id
        self.session.flush()
        return job

    def fail(self, job_id: uuid.UUID, *, failure_code: str) -> Job:
        job = self.get(job_id)
        self._transition(job, JobStatus.FAILED)
        job.failure_code = failure_code
        job.finished_at = dt.datetime.now(dt.UTC)
        self.session.flush()
        return job

    def retry_or_fail(self, job_id: uuid.UUID, *, failure_code: str) -> Job:
        """Return the job to the queue while attempts remain, otherwise fail it terminally."""

        job = self.get(job_id)
        if job.attempts < job.max_attempts:
            self._transition(job, JobStatus.QUEUED)
            job.failure_code = failure_code
            job.started_at = None
            self.session.flush()
            return job
        return self.fail(job_id, failure_code=failure_code)

    def cancel(self, job_id: uuid.UUID) -> Job:
        job = self.get(job_id)
        self._transition(job, JobStatus.CANCELLED)
        job.finished_at = dt.datetime.now(dt.UTC)
        self.session.flush()
        return job

    @staticmethod
    def _transition(job: Job, target: JobStatus) -> None:
        try:
            JOB_STATES.require(job.status.value, target.value)
        except InvalidStateTransition as exc:
            raise exc from None
        job.status = target
