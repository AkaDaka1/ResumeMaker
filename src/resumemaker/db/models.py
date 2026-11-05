"""ORM models.

Design notes
------------
* `ledger_entries` is the single source of truth for the balance. There is no mutable balance
  column on `users`: the balance is always ``SUM(amount)`` over posted journal entries.
* `ledger_entries.operation_id` intentionally has **no** foreign key to `operations`: it is a
  correlation key, and keeping it FK-free avoids a circular dependency between the two tables
  while still letting operations point at the entry they created.
* Enum columns are stored as VARCHAR + CHECK (`native_enum=False`) so adding a member is a normal
  migration instead of a PostgreSQL type change.
* Raw webhook bodies and interview text are never stored here; only hashes and safe summaries.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.orm import Mapped, mapped_column

from resumemaker.db.base import Base
from resumemaker.domain.enums import (
    ConsentKind,
    JobStatus,
    JobType,
    LedgerEntryType,
    OperationStatus,
    OperationType,
    PaymentEventOutcome,
    PaymentMethod,
    PaymentStatus,
    ResumeLanguage,
    ResumeMode,
)


def _created_at() -> Mapped[dt.datetime]:
    return mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


def _enum(enum_type: type, *, length: int) -> SAEnum:
    """Store the enum *value*, not the member name.

    The API and the status text the bot shows are lowercase; persisting names would make the
    CHECK constraint disagree with `server_default` and with every API response.
    """

    return SAEnum(
        enum_type,
        native_enum=False,
        length=length,
        validate_strings=True,
        values_callable=lambda members: [member.value for member in members],
    )


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Telegram ids fit in a signed 64-bit integer. Nullable until the account is linked.
    telegram_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)

    interview_language: Mapped[ResumeLanguage] = mapped_column(
        _enum(ResumeLanguage, length=8),
        nullable=False,
        default=ResumeLanguage.RU,
        server_default=ResumeLanguage.RU.value,
    )
    resume_language: Mapped[ResumeLanguage | None] = mapped_column(
        _enum(ResumeLanguage, length=8),
        nullable=True,
    )

    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    # Set on an explicit deletion request; the retention worker physically removes rows later.
    deleted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (UniqueConstraint("telegram_user_id", name="uq_users_telegram_user_id"),)


class LedgerEntry(Base):
    """Immutable journal entry. Corrections are new entries, never updates."""

    __tablename__ = "ledger_entries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    # Correlation only: no FK to operations, see module docstring.
    operation_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    entry_type: Mapped[LedgerEntryType] = mapped_column(
        _enum(LedgerEntryType, length=24),
        nullable=False,
    )
    # Signed amount in internal credits: positive adds, negative removes.
    amount: Mapped[int] = mapped_column(BigInteger, nullable=False)
    currency: Mapped[str] = mapped_column(
        String(8), nullable=False, default="CREDITS", server_default="CREDITS"
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False)

    #: Globally unique per business intent, e.g. ``debit:<operation_id>``.
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    reverses_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_entries.id", ondelete="SET NULL"), nullable=True
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[dt.datetime] = _created_at()
    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_ledger_entries_idempotency_key"),
        CheckConstraint("amount <> 0", name="amount_not_zero"),
        Index("ix_ledger_entries_user_id_created_at", "user_id", "created_at"),
        Index("ix_ledger_entries_operation_id", "operation_id"),
    )


class Operation(Base):
    """One paid unit of work and its finite lifecycle."""

    __tablename__ = "operations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    operation_type: Mapped[OperationType] = mapped_column(
        _enum(OperationType, length=24), nullable=False
    )
    status: Mapped[OperationStatus] = mapped_column(
        _enum(OperationStatus, length=32),
        nullable=False,
        default=OperationStatus.AWAITING_CONSENT,
        server_default=OperationStatus.AWAITING_CONSENT.value,
    )

    scenario_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scenario_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    resume_language: Mapped[ResumeLanguage] = mapped_column(
        _enum(ResumeLanguage, length=8), nullable=False
    )
    resume_mode: Mapped[ResumeMode] = mapped_column(
        _enum(ResumeMode, length=16),
        nullable=False,
        default=ResumeMode.UNIVERSAL,
        server_default=ResumeMode.UNIVERSAL.value,
    )

    reserved_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    actual_credits: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    balance_debit_entry_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("ledger_entries.id", ondelete="SET NULL"), nullable=True
    )

    #: Makes "start this operation twice" a no-op with the same operation returned.
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_operations_idempotency_key"),
        CheckConstraint("reserved_credits >= 0", name="reserved_credits_non_negative"),
        CheckConstraint(
            "actual_credits IS NULL OR actual_credits >= 0", name="actual_credits_non_negative"
        ),
        Index("ix_operations_user_id_created_at", "user_id", "created_at"),
        Index("ix_operations_status", "status"),
    )


class Consent(Base):
    """One recorded consent decision. History is append-only."""

    __tablename__ = "consents"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[ConsentKind] = mapped_column(
        _enum(ConsentKind, length=32), nullable=False
    )
    granted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    document_version: Mapped[str] = mapped_column(String(32), nullable=False)
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        Index("ix_consents_user_id_kind_created_at", "user_id", "kind", "created_at"),
    )


class Payment(Base):
    """A local payment plus the provider-side session that backs it."""

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    operation_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("operations.id", ondelete="SET NULL"), nullable=True
    )

    method: Mapped[PaymentMethod] = mapped_column(
        _enum(PaymentMethod, length=24), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        _enum(PaymentStatus, length=16),
        nullable=False,
        default=PaymentStatus.PENDING,
        server_default=PaymentStatus.PENDING.value,
    )

    #: Credits the user is buying. Never taken from user input at webhook time.
    amount_credits: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Provider charge currency: XTR for Telegram Stars, RUB for SBP.
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str | None] = mapped_column(String(200), nullable=True)

    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_payment_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(160), nullable=False)

    expires_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    paid_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        UniqueConstraint("provider_payment_id", name="uq_payments_provider_payment_id"),
        UniqueConstraint("idempotency_key", name="uq_payments_idempotency_key"),
        CheckConstraint("amount_credits > 0", name="amount_credits_positive"),
        Index("ix_payments_user_id_status", "user_id", "status"),
        Index("ix_payments_operation_id", "operation_id"),
    )


class PaymentEvent(Base):
    """Audit trail of processed provider webhooks, with de-duplication by provider event id."""

    __tablename__ = "payment_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    payment_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("payments.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[PaymentEventOutcome] = mapped_column(
        _enum(PaymentEventOutcome, length=16),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        UniqueConstraint("provider", "provider_event_id", name="uq_payment_events_provider_event"),
        Index("ix_payment_events_payment_id_created_at", "payment_id", "created_at"),
    )


class Job(Base):
    """A queued unit of work, always tied to exactly one paid operation."""

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    job_type: Mapped[JobType] = mapped_column(
        _enum(JobType, length=24), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        _enum(JobStatus, length=16),
        nullable=False,
        default=JobStatus.QUEUED,
        server_default=JobStatus.QUEUED.value,
    )
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[dt.datetime] = _created_at()
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_jobs_operation_id"),
        CheckConstraint("attempts >= 0", name="attempts_non_negative"),
        CheckConstraint("max_attempts > 0", name="max_attempts_positive"),
        Index("ix_jobs_status_created_at", "status", "created_at"),
    )


class LLMUsage(Base):
    """Provider usage per call, kept separate from the user ledger."""

    __tablename__ = "llm_usage"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    operation_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("operations.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False)
    coefficient_input: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    coefficient_output: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    balance_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False)
    credits_charged: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider_request_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[dt.datetime] = _created_at()

    __table_args__ = (
        CheckConstraint("prompt_tokens >= 0", name="prompt_tokens_non_negative"),
        CheckConstraint("completion_tokens >= 0", name="completion_tokens_non_negative"),
        Index("ix_llm_usage_operation_id", "operation_id"),
    )
