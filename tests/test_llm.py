"""Fake LLM provider and cost conversion."""

from __future__ import annotations

import pytest

from resumemaker.domain.enums import OperationType
from resumemaker.domain.errors import LLMProviderError
from resumemaker.domain.pricing import (
    BALANCE_TOKENS_PER_CREDIT,
    INITIAL_GRANT_CREDITS,
    OPERATION_PRICES,
    operation_price,
    provider_balance_tokens,
    provider_usage_cost_credits,
)
from resumemaker.providers.llm.base import (
    LLMMessage,
    LLMMessageRole,
    LLMRequest,
    LLMUsage,
)
from resumemaker.providers.llm.errors import LLMErrorKind, classify_status
from resumemaker.providers.llm.fake import (
    FakeLLMProvider,
    ScriptedResponse,
    estimate_tokens,
)


def request(**overrides: object) -> LLMRequest:
    defaults: dict[str, object] = {
        "model": "am/deepseek-v4-flash",
        "messages": (
            LLMMessage(role=LLMMessageRole.SYSTEM, content="Be precise."),
            LLMMessage(role=LLMMessageRole.USER, content="Расскажи о проекте."),
        ),
    }
    defaults.update(overrides)
    return LLMRequest(**defaults)  # type: ignore[arg-type]


def test_generate_returns_deterministic_usage_and_request_id() -> None:
    provider = FakeLLMProvider()
    provider.script("hello")

    first = provider.generate(request())
    assert first.text == "hello"
    assert first.model_id == "am/deepseek-v4-flash"
    assert first.provider_request_id == "fake-request-000001"
    assert first.usage.completion_tokens == estimate_tokens("hello")
    assert first.usage.prompt_tokens > 0

    second = provider.generate(request())
    assert second.provider_request_id == "fake-request-000002"
    assert second.text == "ok"  # script exhausted, deterministic default


def test_json_mode_defaults_to_empty_object() -> None:
    provider = FakeLLMProvider()
    assert provider.generate(request(json_mode=True)).text == "{}"


def test_scripted_dict_becomes_structured_data() -> None:
    provider = FakeLLMProvider()
    provider.script({"summary": "ok"})

    response = provider.generate(request(json_mode=True))

    assert response.structured_data == {"summary": "ok"}
    assert response.text == '{"summary": "ok"}'


def test_explicit_usage_is_preserved() -> None:
    provider = FakeLLMProvider()
    provider.script(
        ScriptedResponse(text="answer", usage=LLMUsage(prompt_tokens=10, completion_tokens=5))
    )

    response = provider.generate(request())

    assert response.usage.total_tokens == 15


def test_calls_are_recorded_without_leaking_into_domain() -> None:
    provider = FakeLLMProvider()
    provider.generate(request())

    assert len(provider.calls) == 1
    assert provider.last_request.messages[1].content == "Расскажи о проекте."


def test_scripted_error_is_raised_and_consumed() -> None:
    provider = FakeLLMProvider()
    provider.script_error(429)
    provider.script("recovered")

    with pytest.raises(LLMProviderError) as error:
        provider.generate(request())
    assert error.value.retryable is True
    assert error.value.status_code == 429

    assert provider.generate(request()).text == "recovered"


@pytest.mark.parametrize(
    ("status_code", "code", "kind", "retryable"),
    [
        (400, None, LLMErrorKind.BAD_REQUEST, False),
        (400, "output_budget_exhausted", LLMErrorKind.OUTPUT_BUDGET_EXHAUSTED, False),
        (401, None, LLMErrorKind.AUTHENTICATION, False),
        (402, None, LLMErrorKind.PAYMENT_REQUIRED, False),
        (403, None, LLMErrorKind.FORBIDDEN, False),
        (404, None, LLMErrorKind.NOT_FOUND, False),
        (406, None, LLMErrorKind.UNSUPPORTED, False),
        (422, None, LLMErrorKind.UNSUPPORTED, False),
        (429, None, LLMErrorKind.RATE_LIMIT, True),
        (500, None, LLMErrorKind.SERVER, True),
        (503, "bad_gateway", LLMErrorKind.SERVER, True),
        (503, "gateway_timeout", LLMErrorKind.TIMEOUT, True),
    ],
)
def test_error_classification(
    status_code: int, code: str | None, kind: LLMErrorKind, retryable: bool
) -> None:
    classification = classify_status(status_code, code)

    assert classification.kind is kind
    assert classification.retryable is retryable


def test_rate_limit_carries_retry_after_hint() -> None:
    assert classify_status(429).retry_after_seconds == 30


def test_provider_cost_uses_both_coefficients_and_rounds_up() -> None:
    credits = provider_usage_cost_credits(
        prompt_tokens=100_000,
        completion_tokens=20_000,
        coefficient_input="1.5",
        coefficient_output="3",
    )

    # 150_000 + 60_000 = 210_000 balance tokens -> 21 credits
    assert credits == 21


def test_provider_balance_tokens_follow_the_documented_formula() -> None:
    assert (
        provider_balance_tokens(
            prompt_tokens=10_000,
            completion_tokens=2_000,
            coefficient_input="1.5",
            coefficient_output="1.5",
        )
        == 18_000
    )


def test_provider_balance_tokens_round_up_fractional_coefficients() -> None:
    assert (
        provider_balance_tokens(
            prompt_tokens=1,
            completion_tokens=0,
            coefficient_input="0.05",
            coefficient_output="1",
        )
        == 1
    )


def test_provider_cost_is_at_least_one_credit() -> None:
    assert (
        provider_usage_cost_credits(
            prompt_tokens=1,
            completion_tokens=0,
            coefficient_input="0.05",
            coefficient_output="0.05",
        )
        == 1
    )


def test_provider_cost_for_empty_usage_is_zero() -> None:
    assert (
        provider_usage_cost_credits(
            prompt_tokens=0,
            completion_tokens=0,
            coefficient_input="1",
            coefficient_output="1",
        )
        == 0
    )


def test_conversion_constant_is_documented_and_positive() -> None:
    assert BALANCE_TOKENS_PER_CREDIT > 0


def test_operation_prices_are_known_integers() -> None:
    for operation_type in OperationType:
        price = operation_price(operation_type)
        assert isinstance(price, int)
        assert price > 0


def test_initial_grant_covers_one_preview() -> None:
    assert INITIAL_GRANT_CREDITS >= OPERATION_PRICES[OperationType.PREVIEW]
