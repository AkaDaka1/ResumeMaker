"""Payment service: signed webhooks, reconciliation, de-duplication and refunds."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping

import pytest
from sqlalchemy import func, select

from resumemaker.db.models import Payment, PaymentEvent
from resumemaker.domain.enums import (
    OperationType,
    PaymentEventOutcome,
    PaymentMethod,
    PaymentStatus,
    ResumeLanguage,
)
from resumemaker.domain.errors import (
    IdempotencyConflict,
    NotFound,
    PaymentVerificationError,
)
from resumemaker.providers.payments.fake import FakePaymentProvider
from resumemaker.services.ledger import LedgerService
from resumemaker.services.operations import OperationService, StartOperationRequest
from resumemaker.services.payments import PaymentService, WebhookResult

SECRET = "test-secret"


@pytest.fixture
def provider() -> FakePaymentProvider:
    return FakePaymentProvider(secret=SECRET)


def create_payment(
    uow,
    provider: FakePaymentProvider,
    user_id: uuid.UUID,
    *,
    amount: int = 100,
    method: PaymentMethod = PaymentMethod.TELEGRAM_STARS,
    key: str | None = None,
    operation_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    with uow() as session:
        payment = PaymentService(session, provider).create(
            user_id=user_id,
            method=method,
            amount_credits=amount,
            idempotency_key=key or f"pay-{uuid.uuid4()}",
            operation_id=operation_id,
        )
        assert payment.provider_payment_id is not None
        return payment.id, payment.provider_payment_id


def webhook(
    uow,
    provider: FakePaymentProvider,
    body: bytes,
    headers: Mapping[str, str],
) -> WebhookResult:
    with uow() as session:
        return PaymentService(session, provider).handle_webhook(body, headers)


def balance(uow, user_id: uuid.UUID) -> int:
    with uow() as session:
        return LedgerService(session).balance(user_id)


def test_create_payment_opens_provider_session(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    payment_id, provider_payment_id = create_payment(uow, provider, user_id)

    with uow() as session:
        payment = session.get(Payment, payment_id)
        assert payment is not None
        assert payment.status is PaymentStatus.PENDING
        assert payment.currency == "XTR"
        assert payment.provider == "fake"
        assert payment.provider_payment_url is not None
        assert payment.expires_at is not None

    assert provider_payment_id in provider.sessions()


def test_sbp_payment_uses_rub(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    create_payment(uow, provider, user_id, method=PaymentMethod.SBP)

    with uow() as session:
        payment = session.scalar(select(Payment))
        assert payment is not None
        assert payment.currency == "RUB"


def test_create_payment_is_idempotent(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    key = "same-key"

    first_id, first_provider_id = create_payment(uow, provider, user_id, key=key)
    second_id, second_provider_id = create_payment(uow, provider, user_id, key=key)

    assert first_id == second_id
    assert first_provider_id == second_provider_id
    assert len(provider.sessions()) == 1
    with uow() as session:
        assert session.scalar(select(func.count()).select_from(Payment)) == 1


def test_create_payment_rejects_different_intent_for_same_key(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    key = "same-key"
    create_payment(uow, provider, user_id, key=key, amount=100)

    with pytest.raises(IdempotencyConflict):
        create_payment(uow, provider, user_id, key=key, amount=200)


def test_paid_webhook_grants_credits_exactly_once(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)
    body, headers = provider.build_webhook(provider_payment_id)

    first = webhook(uow, provider, body, headers)
    assert first.outcome is PaymentEventOutcome.APPLIED
    assert balance(uow, user_id) == 100

    # Exact same payload delivered again.
    repeat = webhook(uow, provider, body, headers)
    assert repeat.outcome is PaymentEventOutcome.DUPLICATE
    assert balance(uow, user_id) == 100

    # A new event id for an already paid payment must not credit again either.
    body2, headers2 = provider.build_webhook(provider_payment_id, status=PaymentStatus.PAID)
    second = webhook(uow, provider, body2, headers2)
    assert second.outcome is PaymentEventOutcome.DUPLICATE
    assert balance(uow, user_id) == 100

    with uow() as session:
        payment = session.scalar(select(Payment))
        assert payment is not None
        assert payment.status is PaymentStatus.PAID
        assert payment.paid_at is not None
        applied = session.scalar(
            select(func.count())
            .select_from(PaymentEvent)
            .where(PaymentEvent.outcome == PaymentEventOutcome.APPLIED)
        )
        assert applied == 1


def test_bad_signature_is_refused_before_any_state_change(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)
    body, _ = provider.build_webhook(provider_payment_id)

    with pytest.raises(PaymentVerificationError):
        webhook(uow, provider, body, {"X-Fake-Signature": "0" * 64})

    assert balance(uow, user_id) == 0
    with uow() as session:
        payment = session.scalar(select(Payment))
        assert payment is not None
        assert payment.status is PaymentStatus.PENDING
        assert session.scalar(select(func.count()).select_from(PaymentEvent)) == 0


def test_missing_signature_is_refused(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)
    body, _ = provider.build_webhook(provider_payment_id)

    with pytest.raises(PaymentVerificationError):
        webhook(uow, provider, body, {})


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"amount_credits": 999}, "amount_mismatch"),
        ({"currency": "USD"}, "currency_mismatch"),
        ({"purpose": "something_else"}, "purpose_mismatch"),
        ({"operation_id": uuid.uuid4()}, "operation_mismatch"),
    ],
)
def test_reconciled_fields_must_match(
    uow, make_user, provider, overrides: dict[str, object], reason: str
) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)
    body, headers = provider.build_webhook(provider_payment_id, **overrides)  # type: ignore[arg-type]

    result = webhook(uow, provider, body, headers)

    assert result.outcome is PaymentEventOutcome.REJECTED
    assert result.reason == reason
    assert balance(uow, user_id) == 0
    with uow() as session:
        payment = session.scalar(select(Payment))
        assert payment is not None
        assert payment.status is PaymentStatus.PENDING
        event = session.scalar(select(PaymentEvent))
        assert event is not None
        assert event.outcome is PaymentEventOutcome.REJECTED
        assert event.reason == reason


def test_unknown_payment_is_recorded_and_rejected(uow, make_user, provider) -> None:
    result = webhook(
        uow,
        provider,
        *provider.build_webhook(
            "fakepay_unknown",
            amount_credits=100,
            currency="XTR",
            purpose="credits_purchase",
        ),
    )

    assert result.outcome is PaymentEventOutcome.REJECTED
    assert result.reason == "unknown_payment"
    with uow() as session:
        event = session.scalar(select(PaymentEvent))
        assert event is not None
        assert event.payment_id is None
        assert event.reason == "unknown_payment"


def test_failed_payment_cannot_be_paid_afterwards(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)

    failed = webhook(
        uow, provider, *provider.build_webhook(provider_payment_id, status=PaymentStatus.FAILED)
    )
    assert failed.outcome is PaymentEventOutcome.APPLIED

    paid = webhook(uow, provider, *provider.build_webhook(provider_payment_id))
    assert paid.outcome is PaymentEventOutcome.REJECTED
    assert paid.reason == "payment_failed"
    assert balance(uow, user_id) == 0


def test_expired_event_closes_pending_payment(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    payment_id, provider_payment_id = create_payment(uow, provider, user_id)

    result = webhook(
        uow, provider, *provider.build_webhook(provider_payment_id, status=PaymentStatus.EXPIRED)
    )

    assert result.outcome is PaymentEventOutcome.APPLIED
    with uow() as session:
        payment = session.get(Payment, payment_id)
        assert payment is not None
        assert payment.status is PaymentStatus.EXPIRED


def test_paid_after_local_expiry_needs_review(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    payment_id, provider_payment_id = create_payment(uow, provider, user_id)

    with uow() as session:
        payment = session.get(Payment, payment_id)
        assert payment is not None
        payment.expires_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)

    result = webhook(uow, provider, *provider.build_webhook(provider_payment_id))

    assert result.outcome is PaymentEventOutcome.REJECTED
    assert result.reason == "paid_after_local_expiry"
    assert balance(uow, user_id) == 0
    with uow() as session:
        payment = session.get(Payment, payment_id)
        assert payment is not None
        assert payment.status is PaymentStatus.PENDING


def test_refund_after_paid_reverses_credits(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    payment_id, provider_payment_id = create_payment(uow, provider, user_id)

    webhook(uow, provider, *provider.build_webhook(provider_payment_id))
    assert balance(uow, user_id) == 100

    refund = webhook(
        uow, provider, *provider.build_webhook(provider_payment_id, status=PaymentStatus.REFUNDED)
    )
    assert refund.outcome is PaymentEventOutcome.APPLIED
    assert balance(uow, user_id) == 0

    duplicate = webhook(
        uow, provider, *provider.build_webhook(provider_payment_id, status=PaymentStatus.REFUNDED)
    )
    assert duplicate.outcome is PaymentEventOutcome.DUPLICATE

    with uow() as session:
        payment = session.get(Payment, payment_id)
        assert payment is not None
        assert payment.status is PaymentStatus.REFUNDED

    assert balance(uow, user_id) == 0


def test_refund_of_spent_credits_is_explicitly_negative(uow, make_user, provider) -> None:
    user_id = make_user(credits=0)
    _, provider_payment_id = create_payment(uow, provider, user_id)
    webhook(uow, provider, *provider.build_webhook(provider_payment_id))

    with uow() as session:
        LedgerService(session).debit_for_operation(
            user_id=user_id, operation_id=uuid.uuid4(), amount=100, reason="preview"
        )

    webhook(
        uow,
        provider,
        *provider.build_webhook(provider_payment_id, status=PaymentStatus.REFUNDED),
    )

    assert balance(uow, user_id) == -100


def test_payment_linked_to_foreign_operation_is_refused(uow, make_user, provider) -> None:
    owner = make_user(credits=100)
    stranger = make_user(credits=0)

    with uow() as session:
        operation_id = (
            OperationService(session)
            .start(
                StartOperationRequest(
                    user_id=owner,
                    operation_type=OperationType.PREVIEW,
                    resume_language=ResumeLanguage.RU,
                    idempotency_key=f"op-{uuid.uuid4()}",
                )
            )
            .operation.id
        )

    with pytest.raises(NotFound):
        create_payment(uow, provider, stranger, operation_id=operation_id)
