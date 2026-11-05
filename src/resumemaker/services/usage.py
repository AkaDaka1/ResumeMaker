"""Provider usage accounting.

Provider balance tokens are recorded next to internal credits, never instead of them. This keeps
the "what the gateway charged" and "what the user paid" questions separable during an incident.
"""

from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy.orm import Session

from resumemaker.db.models import LLMUsage as LLMUsageRow
from resumemaker.db.models import Operation
from resumemaker.domain.errors import NotFound
from resumemaker.domain.pricing import (
    provider_balance_tokens,
    provider_usage_cost_credits,
)
from resumemaker.providers.llm.base import LLMUsage


class UsageService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        operation_id: uuid.UUID,
        usage: LLMUsage,
        model_id: str,
        provider: str,
        coefficient_input: Decimal | float | str,
        coefficient_output: Decimal | float | str,
        job_id: uuid.UUID | None = None,
        credits_charged: int | None = None,
        provider_request_id: str | None = None,
    ) -> LLMUsageRow:
        if self.session.get(Operation, operation_id) is None:
            raise NotFound(f"operation {operation_id} does not exist")

        balance_tokens = provider_balance_tokens(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            coefficient_input=coefficient_input,
            coefficient_output=coefficient_output,
        )
        if credits_charged is None:
            credits_charged = provider_usage_cost_credits(
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                coefficient_input=coefficient_input,
                coefficient_output=coefficient_output,
            )

        row = LLMUsageRow(
            operation_id=operation_id,
            job_id=job_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            coefficient_input=Decimal(str(coefficient_input)),
            coefficient_output=Decimal(str(coefficient_output)),
            balance_tokens=balance_tokens,
            credits_charged=credits_charged,
            provider_request_id=provider_request_id,
        )
        self.session.add(row)
        self.session.flush()
        return row
