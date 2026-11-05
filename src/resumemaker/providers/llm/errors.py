"""Provider error taxonomy.

Derived from the documented AnyModel error table. The application uses `retryable` to decide
whether to back off; it must never retry 401/402/403/404/406 automatically, and it must not map
402 ("provider balance is empty") to "the user has no credits".
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LLMErrorKind(StrEnum):
    BAD_REQUEST = "bad_request"
    OUTPUT_BUDGET_EXHAUSTED = "output_budget_exhausted"
    AUTHENTICATION = "authentication"
    PAYMENT_REQUIRED = "payment_required"
    FORBIDDEN = "forbidden"
    NOT_FOUND = "not_found"
    UNSUPPORTED = "unsupported"
    RATE_LIMIT = "rate_limit"
    SERVER = "server"
    TIMEOUT = "timeout"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ErrorClassification:
    kind: LLMErrorKind
    retryable: bool
    retry_after_seconds: int | None = None


_RETRY_AFTER = 30


def classify_status(status_code: int, code: str | None = None) -> ErrorClassification:
    """Classify a provider HTTP failure.

    `output_budget_exhausted` arrives as 400 but has already consumed tokens, so it gets its own
    kind and is not retried automatically.
    """

    if status_code == 400:
        if code == "output_budget_exhausted":
            return ErrorClassification(LLMErrorKind.OUTPUT_BUDGET_EXHAUSTED, retryable=False)
        return ErrorClassification(LLMErrorKind.BAD_REQUEST, retryable=False)
    if status_code == 401:
        return ErrorClassification(LLMErrorKind.AUTHENTICATION, retryable=False)
    if status_code == 402:
        return ErrorClassification(LLMErrorKind.PAYMENT_REQUIRED, retryable=False)
    if status_code == 403:
        return ErrorClassification(LLMErrorKind.FORBIDDEN, retryable=False)
    if status_code == 404:
        return ErrorClassification(LLMErrorKind.NOT_FOUND, retryable=False)
    if status_code in {406, 422}:
        return ErrorClassification(LLMErrorKind.UNSUPPORTED, retryable=False)
    if status_code == 429:
        return ErrorClassification(
            LLMErrorKind.RATE_LIMIT, retryable=True, retry_after_seconds=_RETRY_AFTER
        )
    if status_code in {502, 503, 504}:
        kind = LLMErrorKind.TIMEOUT if code == "gateway_timeout" else LLMErrorKind.SERVER
        return ErrorClassification(kind, retryable=True, retry_after_seconds=1)
    if 500 <= status_code < 600:
        return ErrorClassification(LLMErrorKind.SERVER, retryable=True, retry_after_seconds=1)
    return ErrorClassification(LLMErrorKind.UNKNOWN, retryable=False)
