"""Ledger service.

The balance is a property of the journal, not a column:

    balance(user) = SUM(amount) over ledger_entries where user_id = user

Every write follows the same rules:

1. the user row is locked (`SELECT ... FOR UPDATE`), which serialises concurrent writes for one
   user and makes the overdraft check safe;
2. the idempotency key is checked; an identical repeat returns the original entry, a different
   intent under the same key is an error;
3. a negative entry is refused when it would make the balance negative;
4. entries are appended under a savepoint, so a unique-key race cannot poison the caller's
   transaction.

No method commits: services run inside the caller's unit of work.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from resumemaker.db.models import LedgerEntry, User
from resumemaker.domain.enums import LedgerEntryType
from resumemaker.domain.errors import IdempotencyConflict, InsufficientFunds, NotFound

_POSITIVE_TYPES = {
    LedgerEntryType.INITIAL_GRANT,
    LedgerEntryType.PURCHASE,
    LedgerEntryType.REFUND,
}
_NEGATIVE_TYPES = {LedgerEntryType.DEBIT, LedgerEntryType.EXPIRY}


def debit_key(operation_id: uuid.UUID) -> str:
    return f"debit:{operation_id}"


def refund_key(operation_id: uuid.UUID) -> str:
    return f"refund:{operation_id}"


def adjustment_key(operation_id: uuid.UUID) -> str:
    return f"adjust:{operation_id}"


def purchase_key(payment_id: uuid.UUID) -> str:
    return f"purchase:{payment_id}"


def purchase_reversal_key(payment_id: uuid.UUID) -> str:
    return f"purchase_reversal:{payment_id}"


class LedgerService:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ---------------------------------------------------------------- reads

    def balance(self, user_id: uuid.UUID) -> int:
        total = self.session.scalar(
            select(func.coalesce(func.sum(LedgerEntry.amount), 0)).where(
                LedgerEntry.user_id == user_id
            )
        )
        return int(total or 0)

    def entry_by_key(self, idempotency_key: str) -> LedgerEntry | None:
        return self.session.scalar(
            select(LedgerEntry).where(LedgerEntry.idempotency_key == idempotency_key)
        )

    # --------------------------------------------------------------- writes

    def grant_initial(
        self,
        user_id: uuid.UUID,
        *,
        amount: int,
        reason: str = "initial grant",
        idempotency_key: str | None = None,
        expires_at: dt.datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        return self.post(
            user_id=user_id,
            entry_type=LedgerEntryType.INITIAL_GRANT,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key or f"initial_grant:{user_id}",
            expires_at=expires_at,
            metadata=metadata,
        )

    def purchase(
        self,
        *,
        user_id: uuid.UUID,
        payment_id: uuid.UUID,
        amount: int,
        reason: str = "credits purchase",
    ) -> LedgerEntry:
        return self.post(
            user_id=user_id,
            entry_type=LedgerEntryType.PURCHASE,
            amount=amount,
            reason=reason,
            idempotency_key=purchase_key(payment_id),
            metadata={"payment_id": str(payment_id)},
        )

    def reverse_purchase(
        self,
        *,
        user_id: uuid.UUID,
        payment_id: uuid.UUID,
        amount: int,
        reason: str = "purchase refunded",
    ) -> LedgerEntry:
        """Claw back purchased credits after a provider refund.

        A refund may leave the balance negative when the credits were already spent. That is
        recorded explicitly instead of being hidden; the shortfall is an operational debt.
        """

        original = self.entry_by_key(purchase_key(payment_id))
        entry = self._post(
            user_id=user_id,
            entry_type=LedgerEntryType.ADJUSTMENT,
            amount=-amount,
            reason=reason,
            idempotency_key=purchase_reversal_key(payment_id),
            metadata={"payment_id": str(payment_id), "kind": "purchase_reversal"},
            reverses_entry_id=original.id if original else None,
            allow_negative_balance=True,
        )
        return entry

    def debit_for_operation(
        self,
        *,
        user_id: uuid.UUID,
        operation_id: uuid.UUID,
        amount: int,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        if amount <= 0:
            raise ValueError("debit amount must be positive")
        return self.post(
            user_id=user_id,
            entry_type=LedgerEntryType.DEBIT,
            amount=-amount,
            reason=reason,
            idempotency_key=debit_key(operation_id),
            operation_id=operation_id,
            metadata=metadata,
        )

    def refund_operation(
        self,
        *,
        user_id: uuid.UUID,
        operation_id: uuid.UUID,
        amount: int,
        reason: str,
    ) -> LedgerEntry:
        """Return a reservation in full. Safe to call twice."""

        if amount <= 0:
            raise ValueError("refund amount must be positive")
        original = self.entry_by_key(debit_key(operation_id))
        return self.post(
            user_id=user_id,
            entry_type=LedgerEntryType.REFUND,
            amount=amount,
            reason=reason,
            idempotency_key=refund_key(operation_id),
            operation_id=operation_id,
            reverses_entry_id=original.id if original else None,
            metadata={"kind": "operation_refund"},
        )

    def adjust_operation(
        self,
        *,
        user_id: uuid.UUID,
        operation_id: uuid.UUID,
        reserved: int,
        actual: int,
        reason: str = "operation cost reconciliation",
    ) -> LedgerEntry | None:
        """Reconcile the reservation with the real cost.

        Returns the adjustment entry, or ``None`` when the reservation matched exactly. Calling
        it again with the same ``actual`` is a no-op because the key is derived from the
        operation id.
        """

        if actual < 0:
            raise ValueError("actual cost must be non-negative")
        delta = reserved - actual
        if delta == 0:
            return None
        original = self.entry_by_key(debit_key(operation_id))
        return self._post(
            user_id=user_id,
            entry_type=LedgerEntryType.ADJUSTMENT,
            amount=delta,
            reason=reason,
            idempotency_key=adjustment_key(operation_id),
            operation_id=operation_id,
            reverses_entry_id=original.id if original else None,
            metadata={
                "kind": "operation_adjustment",
                "reserved": reserved,
                "actual": actual,
            },
        )

    def post(
        self,
        *,
        user_id: uuid.UUID,
        entry_type: LedgerEntryType,
        amount: int,
        reason: str,
        idempotency_key: str,
        operation_id: uuid.UUID | None = None,
        reverses_entry_id: uuid.UUID | None = None,
        expires_at: dt.datetime | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> LedgerEntry:
        return self._post(
            user_id=user_id,
            entry_type=entry_type,
            amount=amount,
            reason=reason,
            idempotency_key=idempotency_key,
            operation_id=operation_id,
            reverses_entry_id=reverses_entry_id,
            expires_at=expires_at,
            metadata=metadata,
        )

    # -------------------------------------------------------------- internals

    def _post(
        self,
        *,
        user_id: uuid.UUID,
        entry_type: LedgerEntryType,
        amount: int,
        reason: str,
        idempotency_key: str,
        operation_id: uuid.UUID | None = None,
        reverses_entry_id: uuid.UUID | None = None,
        expires_at: dt.datetime | None = None,
        metadata: dict[str, Any] | None = None,
        allow_negative_balance: bool = False,
    ) -> LedgerEntry:
        if amount == 0:
            raise ValueError("ledger amount must not be zero")
        self._validate_sign(entry_type, amount)
        self._lock_user(user_id)

        values: dict[str, Any] = {
            "user_id": user_id,
            "entry_type": entry_type,
            "amount": amount,
            "reason": reason,
            "idempotency_key": idempotency_key,
            "operation_id": operation_id,
            "reverses_entry_id": reverses_entry_id,
            "expires_at": expires_at,
            "metadata_json": metadata,
        }

        existing = self.entry_by_key(idempotency_key)
        if existing is not None:
            return self._verify_replay(existing, values)

        if amount < 0 and not allow_negative_balance:
            available = self.balance(user_id)
            required = -amount
            if available < required:
                raise InsufficientFunds(user_id=user_id, required=required, available=available)

        try:
            with self.session.begin_nested():
                entry = LedgerEntry(**values)
                self.session.add(entry)
                self.session.flush()
        except IntegrityError:
            # Lost a race on the unique idempotency key: another session posted the same intent.
            existing = self.entry_by_key(idempotency_key)
            if existing is None:
                raise
            return self._verify_replay(existing, values)
        return entry

    def _lock_user(self, user_id: uuid.UUID) -> None:
        statement: Select[tuple[uuid.UUID]] = select(User.id).where(User.id == user_id)
        locked = self.session.scalar(statement.with_for_update())
        if locked is None:
            raise NotFound(f"user {user_id} does not exist")

    def _verify_replay(self, existing: LedgerEntry, values: dict[str, Any]) -> LedgerEntry:
        if (
            existing.user_id != values["user_id"]
            or existing.entry_type != values["entry_type"]
            or existing.amount != values["amount"]
        ):
            raise IdempotencyConflict(
                "idempotency key "
                f"{values['idempotency_key']!r} was already used for a different ledger entry"
            )
        return existing

    @staticmethod
    def _validate_sign(entry_type: LedgerEntryType, amount: int) -> None:
        if entry_type in _POSITIVE_TYPES and amount < 0:
            raise ValueError(f"{entry_type.value} entries must be positive")
        if entry_type in _NEGATIVE_TYPES and amount > 0:
            raise ValueError(f"{entry_type.value} entries must be negative")
