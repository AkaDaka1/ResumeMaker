"""State machines accept only the documented transitions."""

from __future__ import annotations

import pytest

from resumemaker.domain.enums import JobStatus, OperationStatus
from resumemaker.domain.errors import InvalidStateTransition
from resumemaker.domain.states import (
    JOB_STATES,
    OPERATION_STATES,
    PAYMENT_STATES,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("awaiting_consent", "awaiting_payment"),
        ("awaiting_payment", "queued"),
        ("queued", "in_progress"),
        ("in_progress", "preview_ready"),
        ("preview_ready", "awaiting_final_payment"),
        ("awaiting_final_payment", "queued"),
        ("in_progress", "ready"),
    ],
)
def test_operation_happy_paths_are_allowed(current: str, target: str) -> None:
    assert OPERATION_STATES.can(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("ready", "queued"),
        ("failed", "in_progress"),
        ("cancelled", "awaiting_payment"),
        ("preview_ready", "queued"),
    ],
)
def test_operation_backwards_transitions_are_refused(current: str, target: str) -> None:
    assert not OPERATION_STATES.can(current, target)


def test_operation_violation_names_the_entity() -> None:
    with pytest.raises(InvalidStateTransition) as error:
        OPERATION_STATES.require(OperationStatus.READY.value, OperationStatus.QUEUED.value)

    assert error.value.entity == "operation"
    assert error.value.current == "ready"
    assert error.value.target == "queued"


def test_payment_transitions() -> None:
    assert PAYMENT_STATES.can("pending", "paid")
    assert PAYMENT_STATES.can("pending", "expired")
    assert PAYMENT_STATES.can("paid", "refunded")
    assert not PAYMENT_STATES.can("refunded", "paid")
    assert not PAYMENT_STATES.can("failed", "paid")


def test_job_transitions_allow_one_retry_path() -> None:
    assert JOB_STATES.can("queued", "in_progress")
    assert JOB_STATES.can("in_progress", "failed")
    assert JOB_STATES.can("in_progress", "queued")
    assert JOB_STATES.can("failed", "queued")
    assert not JOB_STATES.can("succeeded", "queued")


def test_terminal_states_have_no_exit() -> None:
    assert JOB_STATES.next_states(JobStatus.SUCCEEDED.value) == frozenset()
    assert OPERATION_STATES.next_states(OperationStatus.READY.value) == frozenset()
