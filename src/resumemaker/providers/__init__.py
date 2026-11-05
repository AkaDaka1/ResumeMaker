"""Provider adapters.

Domain code talks to protocols here; concrete provider payload shapes never leak upward.
Sprint 1 ships fake implementations only.
"""

from resumemaker.providers.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMUsage
from resumemaker.providers.payments.base import (
    PaymentProvider,
    PaymentSessionRequest,
    PaymentSessionResult,
    ProviderPaymentEvent,
)

__all__ = [
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "PaymentProvider",
    "PaymentSessionRequest",
    "PaymentSessionResult",
    "ProviderPaymentEvent",
]
