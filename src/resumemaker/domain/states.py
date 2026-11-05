"""Explicit finite state machines.

States are not inferred from free text and are not changed by providers. Any transition that
is not listed here raises `InvalidStateTransition`, which keeps the bot script honest about what
the user can be told.
"""

from __future__ import annotations

from collections.abc import Mapping

from resumemaker.domain.enums import JobStatus, OperationStatus, PaymentStatus
from resumemaker.domain.errors import InvalidStateTransition

TransitionMap = Mapping[str, frozenset[str]]


class StateMachine:
    """A named transition table over an enum of states."""

    def __init__(self, name: str, states: type, transitions: TransitionMap) -> None:
        self.name = name
        self.states = states
        self._transitions = {state: frozenset(targets) for state, targets in transitions.items()}
        unknown = set(self._transitions) - {item.value for item in states}
        if unknown:
            raise ValueError(f"{name}: unknown states in transition table: {sorted(unknown)}")

    def can(self, current: str | None, target: str | None) -> bool:
        if current is None or target is None:
            return False
        return target in self._transitions.get(current, frozenset())

    def require(self, current: str | None, target: str | None) -> None:
        if not self.can(current, target):
            raise InvalidStateTransition(
                entity=self.name, current=str(current), target=str(target)
            )

    def next_states(self, current: str) -> frozenset[str]:
        return self._transitions.get(current, frozenset())


OPERATION_STATES = StateMachine(
    "operation",
    OperationStatus,
    {
        OperationStatus.AWAITING_CONSENT.value: frozenset(
            {
                OperationStatus.AWAITING_PAYMENT.value,
                OperationStatus.CANCELLED.value,
                OperationStatus.FAILED.value,
            }
        ),
        OperationStatus.AWAITING_PAYMENT.value: frozenset(
            {
                OperationStatus.QUEUED.value,
                OperationStatus.CANCELLED.value,
                OperationStatus.FAILED.value,
            }
        ),
        OperationStatus.QUEUED.value: frozenset(
            {
                OperationStatus.IN_PROGRESS.value,
                OperationStatus.FAILED.value,
                OperationStatus.CANCELLED.value,
            }
        ),
        OperationStatus.IN_PROGRESS.value: frozenset(
            {
                OperationStatus.PREVIEW_READY.value,
                OperationStatus.READY.value,
                OperationStatus.FAILED.value,
            }
        ),
        OperationStatus.PREVIEW_READY.value: frozenset(
            {
                OperationStatus.AWAITING_FINAL_PAYMENT.value,
                OperationStatus.READY.value,
                OperationStatus.CANCELLED.value,
            }
        ),
        OperationStatus.AWAITING_FINAL_PAYMENT.value: frozenset(
            {
                OperationStatus.QUEUED.value,
                OperationStatus.READY.value,
                OperationStatus.FAILED.value,
                OperationStatus.CANCELLED.value,
            }
        ),
        OperationStatus.READY.value: frozenset(),
        OperationStatus.FAILED.value: frozenset(),
        OperationStatus.CANCELLED.value: frozenset(),
    },
)

PAYMENT_STATES = StateMachine(
    "payment",
    PaymentStatus,
    {
        PaymentStatus.PENDING.value: frozenset(
            {
                PaymentStatus.PAID.value,
                PaymentStatus.FAILED.value,
                PaymentStatus.EXPIRED.value,
            }
        ),
        PaymentStatus.PAID.value: frozenset({PaymentStatus.REFUNDED.value}),
        PaymentStatus.FAILED.value: frozenset(),
        PaymentStatus.EXPIRED.value: frozenset(),
        PaymentStatus.REFUNDED.value: frozenset(),
    },
)

JOB_STATES = StateMachine(
    "job",
    JobStatus,
    {
        JobStatus.QUEUED.value: frozenset(
            {JobStatus.IN_PROGRESS.value, JobStatus.CANCELLED.value, JobStatus.FAILED.value}
        ),
        JobStatus.IN_PROGRESS.value: frozenset(
            {
                JobStatus.SUCCEEDED.value,
                JobStatus.FAILED.value,
                JobStatus.QUEUED.value,
                JobStatus.CANCELLED.value,
            }
        ),
        JobStatus.SUCCEEDED.value: frozenset(),
        JobStatus.FAILED.value: frozenset({JobStatus.QUEUED.value}),
        JobStatus.CANCELLED.value: frozenset(),
    },
)
