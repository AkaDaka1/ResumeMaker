"""Provider usage is stored separately from the user ledger."""

from __future__ import annotations

import uuid

import pytest

from resumemaker.domain.enums import OperationType, ResumeLanguage
from resumemaker.domain.errors import NotFound
from resumemaker.providers.llm.base import LLMUsage
from resumemaker.services.operations import OperationService, StartOperationRequest
from resumemaker.services.usage import UsageService


def start_operation(uow, user_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    with uow() as session:
        result = OperationService(session).start(
            StartOperationRequest(
                user_id=user_id,
                operation_type=OperationType.PREVIEW,
                resume_language=ResumeLanguage.RU,
                idempotency_key=f"usage-{uuid.uuid4()}",
            )
        )
        return result.operation.id, result.job.id


def test_usage_row_separates_balance_tokens_from_credits(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id, job_id = start_operation(uow, user_id)

    with uow() as session:
        row = UsageService(session).record(
            operation_id=operation_id,
            job_id=job_id,
            usage=LLMUsage(prompt_tokens=100_000, completion_tokens=20_000),
            model_id="am/deepseek-v4-flash",
            provider="fake",
            coefficient_input="1.5",
            coefficient_output="3",
            provider_request_id="fake-request-000001",
        )
        assert row.balance_tokens == 210_000
        assert row.credits_charged == 21
        assert row.provider_request_id == "fake-request-000001"


def test_explicit_credits_are_kept(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id, _ = start_operation(uow, user_id)

    with uow() as session:
        row = UsageService(session).record(
            operation_id=operation_id,
            usage=LLMUsage(prompt_tokens=10, completion_tokens=10),
            model_id="am/deepseek-v4-flash",
            provider="fake",
            coefficient_input="1",
            coefficient_output="1",
            credits_charged=5,
        )
        assert row.credits_charged == 5


def test_usage_requires_an_existing_operation(uow) -> None:
    with pytest.raises(NotFound):
        with uow() as session:
            UsageService(session).record(
                operation_id=uuid.uuid4(),
                usage=LLMUsage(prompt_tokens=1, completion_tokens=1),
                model_id="am/deepseek-v4-flash",
                provider="fake",
                coefficient_input="1",
                coefficient_output="1",
            )
