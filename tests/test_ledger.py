"""Ledger invariants: idempotency, overdraft protection under concurrency, reconciliation."""

from __future__ import annotations

import threading
import uuid
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from resumemaker.db.models import LedgerEntry
from resumemaker.domain.enums import LedgerEntryType
from resumemaker.domain.errors import (
    IdempotencyConflict,
    InsufficientFunds,
    NotFound,
)
from resumemaker.services.ledger import LedgerService


def _balance(uow, user_id: uuid.UUID) -> int:
    with uow() as session:
        return LedgerService(session).balance(user_id)


def test_grant_adds_credits(uow, make_user) -> None:
    user_id = make_user(credits=100)

    assert _balance(uow, user_id) == 100


def test_user_without_entries_has_zero_balance(uow, make_user) -> None:
    user_id = make_user(credits=0)

    assert _balance(uow, user_id) == 0


def test_grant_is_idempotent(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with uow() as session:
        service = LedgerService(session)
        first = service.grant_initial(user_id, amount=100)
        second = service.grant_initial(user_id, amount=100)
        assert first.id == second.id

    assert _balance(uow, user_id) == 100


def test_debit_is_idempotent_by_operation(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        first = service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=40, reason="preview"
        )
        second = service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=40, reason="preview"
        )
        assert first.id == second.id

    with uow() as session:
        debits = session.scalar(
            select(func.count())
            .select_from(LedgerEntry)
            .where(LedgerEntry.entry_type == LedgerEntryType.DEBIT)
        )
        assert debits == 1

    assert _balance(uow, user_id) == 60


def test_reusing_key_for_different_amount_conflicts(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id = uuid.uuid4()

    with uow() as session:
        LedgerService(session).debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=40, reason="preview"
        )

    with pytest.raises(IdempotencyConflict):
        with uow() as session:
            LedgerService(session).debit_for_operation(
                user_id=user_id, operation_id=operation_id, amount=50, reason="preview"
            )

    assert _balance(uow, user_id) == 60


def test_debit_above_balance_is_refused(uow, make_user) -> None:
    user_id = make_user(credits=10)

    with pytest.raises(InsufficientFunds) as error:
        with uow() as session:
            LedgerService(session).debit_for_operation(
                user_id=user_id, operation_id=uuid.uuid4(), amount=20, reason="preview"
            )

    assert error.value.required == 20
    assert error.value.available == 10
    assert _balance(uow, user_id) == 10


def test_concurrent_debits_cannot_overdraw(uow, make_user) -> None:
    """Two sessions try to spend 80 of 100 at the same time; exactly one must win."""

    user_id = make_user(credits=100)
    barrier = threading.Barrier(2)
    results: list[tuple[str, uuid.UUID]] = []
    errors: list[BaseException] = []

    def spend(operation_id: uuid.UUID) -> None:
        try:
            with uow() as session:
                barrier.wait(timeout=10)
                LedgerService(session).debit_for_operation(
                    user_id=user_id, operation_id=operation_id, amount=80, reason="race"
                )
            results.append(("ok", operation_id))
        except InsufficientFunds:
            results.append(("insufficient", operation_id))
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
            results.append(("error", operation_id))

    threads = [threading.Thread(target=spend, args=(uuid.uuid4(),)) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)

    assert not errors
    assert sorted(kind for kind, _ in results) == ["insufficient", "ok"]
    assert _balance(uow, user_id) == 20


def test_refund_restores_reservation_once(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="preview"
        )
        first = service.refund_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="failed"
        )
        second = service.refund_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="failed"
        )
        assert first.id == second.id

    assert _balance(uow, user_id) == 100


def test_adjustment_returns_the_difference(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="preview"
        )
        adjustment = service.adjust_operation(
            user_id=user_id, operation_id=operation_id, reserved=100, actual=30
        )
        assert adjustment is not None
        assert adjustment.amount == 70
        assert adjustment.entry_type is LedgerEntryType.ADJUSTMENT

        again = service.adjust_operation(
            user_id=user_id, operation_id=operation_id, reserved=100, actual=30
        )
        assert again is not None
        assert again.id == adjustment.id

    assert _balance(uow, user_id) == 70


def test_matching_actual_cost_creates_no_adjustment(uow, make_user) -> None:
    user_id = make_user(credits=100)
    operation_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="preview"
        )
        assert (
            service.adjust_operation(
                user_id=user_id, operation_id=operation_id, reserved=100, actual=100
            )
            is None
        )

    assert _balance(uow, user_id) == 0


def test_adjustment_upwards_needs_available_credits(uow, make_user) -> None:
    user_id = make_user(credits=120)
    operation_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        service.debit_for_operation(
            user_id=user_id, operation_id=operation_id, amount=100, reason="preview"
        )

    with pytest.raises(InsufficientFunds):
        with uow() as session:
            LedgerService(session).adjust_operation(
                user_id=user_id, operation_id=operation_id, reserved=100, actual=150
            )

    assert _balance(uow, user_id) == 20


def test_purchase_then_reversal_can_leave_negative_balance(uow, make_user) -> None:
    user_id = make_user(credits=100)
    payment_id = uuid.uuid4()

    with uow() as session:
        service = LedgerService(session)
        service.purchase(user_id=user_id, payment_id=payment_id, amount=50)
        service.debit_for_operation(
            user_id=user_id, operation_id=uuid.uuid4(), amount=150, reason="final"
        )
        reversal = service.reverse_purchase(
            user_id=user_id, payment_id=payment_id, amount=50
        )
        assert reversal.amount == -50

    # The shortfall is explicit rather than silently swallowed.
    assert _balance(uow, user_id) == -50


def test_expiry_entry_cannot_overdraw(uow, make_user) -> None:
    user_id = make_user(credits=40)

    with pytest.raises(InsufficientFunds):
        with uow() as session:
            LedgerService(session).post(
                user_id=user_id,
                entry_type=LedgerEntryType.EXPIRY,
                amount=-50,
                reason="credits expired",
                idempotency_key=f"expiry:{uuid.uuid4()}",
            )

    assert _balance(uow, user_id) == 40


def test_unknown_user_is_reported(uow) -> None:
    with pytest.raises(NotFound):
        with uow() as session:
            LedgerService(session).debit_for_operation(
                user_id=uuid.uuid4(),
                operation_id=uuid.uuid4(),
                amount=10,
                reason="preview",
            )


def test_ledger_amounts_are_integer_credits(uow, make_user) -> None:
    """Balance arithmetic is integer credits; provider coefficients never enter the journal."""

    user_id = make_user(credits=100)
    with uow() as session:
        entries = session.scalars(
            select(LedgerEntry).where(LedgerEntry.user_id == user_id)
        ).all()
        assert Decimal(str(entries[0].amount)) == Decimal("100")
        assert all(isinstance(entry.amount, int) for entry in entries)
