"""Domain errors.

Every error that crosses a service boundary is explicit. Application code must map these to
safe user-facing messages without leaking provider codes, prompts or PII.
"""

from __future__ import annotations


class DomainError(Exception):
    """Base class for all expected business failures."""


class InsufficientFunds(DomainError):
    """The user balance does not cover the requested debit."""

    def __init__(self, *, user_id: object, required: int, available: int) -> None:
        super().__init__(
            f"insufficient funds: required={required} available={available}"
        )
        self.user_id = user_id
        self.required = required
        self.available = available


class IdempotencyConflict(DomainError):
    """The same idempotency key was reused for a different business intent."""


class InvalidStateTransition(DomainError):
    """A state machine refused a transition."""

    def __init__(self, *, entity: str, current: str, target: str) -> None:
        super().__init__(f"{entity}: cannot move from {current!r} to {target!r}")
        self.entity = entity
        self.current = current
        self.target = target


class NotFound(DomainError):
    """A referenced entity does not exist."""


class PaymentVerificationError(DomainError):
    """The provider webhook signature or payload could not be verified."""


class PaymentRejected(DomainError):
    """A verified payment event contradicts the locally created payment."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"payment rejected: {reason}")
        self.reason = reason


class OperationNotStartable(DomainError):
    """The operation is not in a state that allows starting work."""


class ProviderError(DomainError):
    """Base class for provider-side failures (LLM, payments)."""


class LLMProviderError(ProviderError):
    """A classified failure returned by the LLM provider.

    `kind` follows the AnyModel error table so the application can decide whether a retry
    is allowed without inspecting provider-specific payloads.
    """

    def __init__(
        self,
        *,
        kind: str,
        status_code: int | None = None,
        code: str | None = None,
        provider_request_id: str | None = None,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        safe_message: str | None = None,
    ) -> None:
        super().__init__(f"llm provider error: {kind}")
        self.kind = kind
        self.status_code = status_code
        self.code = code
        self.provider_request_id = provider_request_id
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.safe_message = safe_message
