"""Payment service.

Rules enforced here, not in the bot script:

* a local payment is created once per idempotency key;
* a payment is confirmed only by a signature-verified provider event, reconciled field by field;
* every provider event is stored once (unique `provider` + `provider_event_id`);
* credits are granted with a ledger idempotency key derived from the payment, so even a missed
  de-duplication check cannot double-credit;
* a refund is a separate, explicit state change that claws the credits back.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from resumemaker.db.models import Operation, Payment, PaymentEvent
from resumemaker.domain.enums import (
    PaymentEventOutcome,
    PaymentMethod,
    PaymentStatus,
)
from resumemaker.domain.errors import (
    IdempotencyConflict,
    NotFound,
)
from resumemaker.domain.states import PAYMENT_STATES
from resumemaker.providers.payments.base import (
    PaymentProvider,
    PaymentSessionRequest,
    ProviderPaymentEvent,
)
from resumemaker.services.ledger import LedgerService

CURRENCY_FOR_METHOD: dict[PaymentMethod, str] = {
    PaymentMethod.TELEGRAM_STARS: "XTR",
    PaymentMethod.SBP: "RUB",
}

PURPOSE_CREDITS_PURCHASE = "credits_purchase"

DEFAULT_SESSION_TTL_SECONDS = 900


@dataclass(frozen=True)
class WebhookResult:
    outcome: PaymentEventOutcome
    payment_id: uuid.UUID | None = None
    reason: str | None = None

    @property
    def applied(self) -> bool:
        return self.outcome is PaymentEventOutcome.APPLIED


class PaymentService:
    def __init__(
        self,
        session: Session,
        provider: PaymentProvider,
        *,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self.session = session
        self.provider = provider
        self.ledger = LedgerService(session)
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))

    # ---------------------------------------------------------------- reads

    def get(self, payment_id: uuid.UUID) -> Payment:
        payment = self.session.get(Payment, payment_id)
        if payment is None:
            raise NotFound(f"payment {payment_id} does not exist")
        return payment

    # --------------------------------------------------------------- writes

    def create(
        self,
        *,
        user_id: uuid.UUID,
        method: PaymentMethod,
        amount_credits: int,
        idempotency_key: str,
        operation_id: uuid.UUID | None = None,
        purpose: str = PURPOSE_CREDITS_PURCHASE,
        description: str | None = None,
        ttl_seconds: int = DEFAULT_SESSION_TTL_SECONDS,
    ) -> Payment:
        if amount_credits <= 0:
            raise ValueError("amount_credits must be positive")
        if not idempotency_key.strip():
            raise ValueError("idempotency_key is required")
        currency = CURRENCY_FOR_METHOD[method]

        existing = self.session.scalar(
            select(Payment).where(Payment.idempotency_key == idempotency_key)
        )
        if existing is not None:
            if (
                existing.user_id != user_id
                or existing.method is not method
                or existing.amount_credits != amount_credits
                or existing.operation_id != operation_id
                or existing.purpose != purpose
            ):
                raise IdempotencyConflict(
                    f"payment idempotency key {idempotency_key!r} "
                    "was already used for a different payment"
                )
            return existing

        if operation_id is not None:
            operation = self.session.get(Operation, operation_id)
            if operation is None or operation.user_id != user_id:
                raise NotFound(f"operation {operation_id} does not belong to user {user_id}")

        with self.session.begin_nested():
            payment = Payment(
                user_id=user_id,
                operation_id=operation_id,
                method=method,
                provider=getattr(self.provider, "name", "unknown"),
                status=PaymentStatus.PENDING,
                amount_credits=amount_credits,
                currency=currency,
                purpose=purpose,
                description=description,
                idempotency_key=idempotency_key,
            )
            self.session.add(payment)
            self.session.flush()

            session_result = self.provider.create_session(
                PaymentSessionRequest(
                    payment_id=payment.id,
                    amount_credits=amount_credits,
                    currency=currency,
                    purpose=purpose,
                    description=description,
                    operation_id=operation_id,
                    ttl_seconds=ttl_seconds,
                )
            )
            payment.provider_payment_id = session_result.provider_payment_id
            payment.provider_payment_url = session_result.payment_url
            payment.expires_at = session_result.expires_at
            self.session.flush()
        return payment

    def handle_webhook(self, body: bytes, headers: Mapping[str, str]) -> WebhookResult:
        event = self.provider.verify_webhook(body, headers)

        try:
            with self.session.begin_nested():
                if self._event_already_processed(event):
                    return WebhookResult(
                        PaymentEventOutcome.DUPLICATE, reason="already_processed"
                    )

                payment = self.session.scalar(
                    select(Payment)
                    .where(Payment.provider_payment_id == event.provider_payment_id)
                    .with_for_update()
                )
                if payment is None:
                    self._record(event, None, PaymentEventOutcome.REJECTED, "unknown_payment")
                    self.session.flush()
                    return WebhookResult(
                        PaymentEventOutcome.REJECTED, reason="unknown_payment"
                    )

                mismatch = self._mismatch(payment, event)
                if mismatch is not None:
                    self._record(event, payment.id, PaymentEventOutcome.REJECTED, mismatch)
                    self.session.flush()
                    return WebhookResult(
                        PaymentEventOutcome.REJECTED,
                        payment_id=payment.id,
                        reason=mismatch,
                    )

                outcome, reason = self._apply(payment, event)
                self._record(event, payment.id, outcome, reason)
                self.session.flush()
                return WebhookResult(outcome, payment_id=payment.id, reason=reason)
        except IntegrityError:
            # Another worker stored the same event id first. Its effects are committed or will be;
            # this request must not apply anything twice.
            self.session.expire_all()
            return WebhookResult(PaymentEventOutcome.DUPLICATE, reason="concurrent_duplicate")

    # -------------------------------------------------------------- internals

    def _apply(
        self, payment: Payment, event: ProviderPaymentEvent
    ) -> tuple[PaymentEventOutcome, str | None]:
        if event.status is PaymentStatus.PAID:
            return self._apply_paid(payment, event)
        if event.status in {PaymentStatus.FAILED, PaymentStatus.EXPIRED}:
            if payment.status is not PaymentStatus.PENDING:
                return PaymentEventOutcome.DUPLICATE, f"already_{payment.status.value}"
            self._transition(payment, event.status)
            return PaymentEventOutcome.APPLIED, None
        if event.status is PaymentStatus.REFUNDED:
            return self._apply_refunded(payment, event)
        return PaymentEventOutcome.REJECTED, f"unsupported_status_{event.status.value}"

    def _apply_paid(
        self, payment: Payment, event: ProviderPaymentEvent
    ) -> tuple[PaymentEventOutcome, str | None]:
        if payment.status is PaymentStatus.PAID:
            return PaymentEventOutcome.DUPLICATE, "already_paid"
        if payment.status is not PaymentStatus.PENDING:
            return PaymentEventOutcome.REJECTED, f"payment_{payment.status.value}"
        expires_at = payment.expires_at
        if expires_at is not None and self._clock() > expires_at:
            # A late confirmation is not thrown away silently: the state is left for review.
            return PaymentEventOutcome.REJECTED, "paid_after_local_expiry"

        self._transition(payment, PaymentStatus.PAID)
        payment.paid_at = self._clock()
        self.ledger.purchase(
            user_id=payment.user_id,
            payment_id=payment.id,
            amount=payment.amount_credits,
        )
        return PaymentEventOutcome.APPLIED, None

    def _apply_refunded(
        self, payment: Payment, event: ProviderPaymentEvent
    ) -> tuple[PaymentEventOutcome, str | None]:
        if payment.status is PaymentStatus.REFUNDED:
            return PaymentEventOutcome.DUPLICATE, "already_refunded"
        if payment.status is not PaymentStatus.PAID:
            return PaymentEventOutcome.REJECTED, f"payment_{payment.status.value}"
        self._transition(payment, PaymentStatus.REFUNDED)
        self.ledger.reverse_purchase(
            user_id=payment.user_id,
            payment_id=payment.id,
            amount=payment.amount_credits,
        )
        return PaymentEventOutcome.APPLIED, None

    def _mismatch(self, payment: Payment, event: ProviderPaymentEvent) -> str | None:
        if payment.provider != event.provider:
            return "provider_mismatch"
        if payment.amount_credits != event.amount_credits:
            return "amount_mismatch"
        if payment.currency != event.currency:
            return "currency_mismatch"
        if payment.purpose != event.purpose:
            return "purpose_mismatch"
        if payment.operation_id != event.operation_id:
            return "operation_mismatch"
        return None

    def _event_already_processed(self, event: ProviderPaymentEvent) -> bool:
        return (
            self.session.scalar(
                select(PaymentEvent.id).where(
                    PaymentEvent.provider == event.provider,
                    PaymentEvent.provider_event_id == event.event_id,
                )
            )
            is not None
        )

    def _record(
        self,
        event: ProviderPaymentEvent,
        payment_id: uuid.UUID | None,
        outcome: PaymentEventOutcome,
        reason: str | None,
    ) -> PaymentEvent:
        record = PaymentEvent(
            payment_id=payment_id,
            provider=event.provider,
            provider_event_id=event.event_id,
            event_type=event.event_type,
            outcome=outcome,
            reason=reason,
            payload_sha256=event.payload_sha256,
        )
        self.session.add(record)
        return record

    @staticmethod
    def _transition(payment: Payment, target: PaymentStatus) -> None:
        PAYMENT_STATES.require(payment.status.value, target.value)
        payment.status = target
