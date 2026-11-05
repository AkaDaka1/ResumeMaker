"""Domain enums shared by the application, the database and the tests.

All values are explicit strings: they are stored in PostgreSQL and exposed to operators,
so renaming a member must never silently change an already persisted value.
"""

from __future__ import annotations

from enum import StrEnum


class OperationType(StrEnum):
    """A billable unit of work. Each type has its own price in `domain.pricing`."""

    PREVIEW = "preview"
    FINAL = "final"
    RENORMALIZATION = "renormalization"


class OperationStatus(StrEnum):
    """Lifecycle of a paid operation.

    The set matches the statuses the bot script is allowed to display, plus `cancelled`
    for an explicit user cancel and `failed` for a terminal technical failure.
    """

    AWAITING_CONSENT = "awaiting_consent"
    AWAITING_PAYMENT = "awaiting_payment"
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    PREVIEW_READY = "preview_ready"
    AWAITING_FINAL_PAYMENT = "awaiting_final_payment"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LedgerEntryType(StrEnum):
    """Journal entry types.

    `initial_grant` and `purchase` add credits, `debit` removes them, `refund` returns a
    reservation, `adjustment` reconciles a reservation with the actual cost, `expiry`
    removes credits that reached their `expires_at`.
    """

    INITIAL_GRANT = "initial_grant"
    PURCHASE = "purchase"
    DEBIT = "debit"
    REFUND = "refund"
    ADJUSTMENT = "adjustment"
    EXPIRY = "expiry"


class ConsentKind(StrEnum):
    """Consents are recorded separately and never bundled into one checkbox."""

    DATA_PROCESSING = "data_processing"
    EXTERNAL_LLM = "external_llm"


class PaymentMethod(StrEnum):
    TELEGRAM_STARS = "telegram_stars"
    SBP = "sbp"


class PaymentStatus(StrEnum):
    PENDING = "pending"
    PAID = "paid"
    FAILED = "failed"
    EXPIRED = "expired"
    REFUNDED = "refunded"


class JobType(StrEnum):
    INTERVIEW = "interview"
    NORMALIZATION = "normalization"
    RENDER_PREVIEW = "render_preview"
    RENDER_FINAL = "render_final"


class JobStatus(StrEnum):
    QUEUED = "queued"
    IN_PROGRESS = "in_progress"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResumeLanguage(StrEnum):
    RU = "ru"
    EN = "en"


class ResumeMode(StrEnum):
    UNIVERSAL = "universal"
    VACANCY = "vacancy"


class PaymentEventOutcome(StrEnum):
    """Result of processing one provider webhook. Events are never deleted."""

    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"
