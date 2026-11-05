"""Pricing in internal credits.

Internal credits are *not* AnyModel balance tokens and are never derived from a user-supplied
number. Prices are integers: rounding happens once, at the boundary, with `ceil`.
"""

from __future__ import annotations

from decimal import Decimal
from math import ceil

from resumemaker.domain.enums import OperationType

#: Credits granted to a new user. The product decision requires this to cover one preview.
INITIAL_GRANT_CREDITS = 100

#: Price of one paid operation, in internal credits.
OPERATION_PRICES: dict[OperationType, int] = {
    OperationType.PREVIEW: 100,
    OperationType.FINAL: 250,
    OperationType.RENORMALIZATION: 60,
}

#: How many provider balance tokens one internal credit is worth. Kept as a constant so the
#: conversion is deterministic and auditable; a change must be a versioned pricing change.
BALANCE_TOKENS_PER_CREDIT = 10_000


class UnknownOperationType(ValueError):
    """Raised when no price is configured for an operation type."""


def operation_price(operation_type: OperationType) -> int:
    try:
        return OPERATION_PRICES[operation_type]
    except KeyError as exc:  # pragma: no cover - defensive, enums are exhaustive today
        raise UnknownOperationType(str(operation_type)) from exc


def provider_balance_tokens(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    coefficient_input: Decimal | float | str,
    coefficient_output: Decimal | float | str,
) -> int:
    """Provider charge in balance tokens, using the documented formula.

    ``ceil(prompt_tokens * coefficient.input + completion_tokens * coefficient.output)``
    """

    if prompt_tokens < 0 or completion_tokens < 0:
        raise ValueError("token counts must be non-negative")
    coefficient_in = Decimal(str(coefficient_input))
    coefficient_out = Decimal(str(coefficient_output))
    if coefficient_in < 0 or coefficient_out < 0:
        raise ValueError("coefficients must be non-negative")

    billed_tokens = Decimal(prompt_tokens) * coefficient_in + Decimal(
        completion_tokens
    ) * coefficient_out
    if billed_tokens <= 0:
        return 0
    return ceil(billed_tokens)


def provider_usage_cost_credits(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    coefficient_input: Decimal | float | str,
    coefficient_output: Decimal | float | str,
) -> int:
    """Convert provider usage into internal credits.

    A non-zero call costs at least one credit, even when the converted amount rounds to zero.
    """

    billed_tokens = provider_balance_tokens(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        coefficient_input=coefficient_input,
        coefficient_output=coefficient_output,
    )
    if billed_tokens <= 0:
        return 0
    return max(1, ceil(Decimal(billed_tokens) / Decimal(BALANCE_TOKENS_PER_CREDIT)))
