"""Fake LLM provider.

No network calls. Responses are scripted, usage is deterministic, and failures use the same
classified error type as a real adapter would, so retry and refund logic can be tested without
touching AnyModel.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from resumemaker.domain.errors import LLMProviderError
from resumemaker.providers.llm.base import LLMRequest, LLMResponse, LLMUsage
from resumemaker.providers.llm.errors import classify_status


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: roughly four characters per token, at least one."""

    return max(1, (len(text) + 3) // 4)


@dataclass(frozen=True)
class ScriptedResponse:
    text: str
    usage: LLMUsage | None = None
    finish_reason: str | None = "stop"
    structured_data: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScriptedError:
    status_code: int
    code: str | None = None
    retry_after_seconds: int | None = None

    def to_error(self, *, provider_request_id: str | None = None) -> LLMProviderError:
        classification = classify_status(self.status_code, self.code)
        return LLMProviderError(
            kind=classification.kind.value,
            status_code=self.status_code,
            code=self.code,
            provider_request_id=provider_request_id,
            retryable=classification.retryable,
            retry_after_seconds=(
                self.retry_after_seconds
                if self.retry_after_seconds is not None
                else classification.retry_after_seconds
            ),
            safe_message=f"provider returned HTTP {self.status_code}",
        )


ScriptItem = ScriptedResponse | ScriptedError | str | dict[str, Any]


class FakeLLMProvider:
    name = "fake"

    def __init__(
        self,
        *,
        model_id: str = "am/deepseek-v4-flash",
        coefficient_input: Decimal | str | float = Decimal("0.05"),
        coefficient_output: Decimal | str | float = Decimal("0.15"),
    ) -> None:
        self.model_id = model_id
        self.coefficient_input = Decimal(str(coefficient_input))
        self.coefficient_output = Decimal(str(coefficient_output))
        self._script: list[ScriptItem] = []
        self._calls: list[LLMRequest] = []
        self._counter = 0

    # ---------------------------------------------------------- test helpers

    def script(self, *items: ScriptItem) -> FakeLLMProvider:
        self._script.extend(items)
        return self

    def script_error(
        self,
        status_code: int,
        *,
        code: str | None = None,
        retry_after_seconds: int | None = None,
    ) -> FakeLLMProvider:
        self._script.append(
            ScriptedError(
                status_code=status_code,
                code=code,
                retry_after_seconds=retry_after_seconds,
            )
        )
        return self

    def reset(self) -> None:
        self._script.clear()
        self._calls.clear()
        self._counter = 0

    @property
    def calls(self) -> Sequence[LLMRequest]:
        return tuple(self._calls)

    @property
    def last_request(self) -> LLMRequest:
        if not self._calls:
            raise LookupError("the fake provider has not been called yet")
        return self._calls[-1]

    # ------------------------------------------------------------- protocol

    def generate(self, request: LLMRequest) -> LLMResponse:
        self._calls.append(request)
        self._counter += 1
        request_id = f"fake-request-{self._counter:06d}"

        item: ScriptItem
        if self._script:
            item = self._script.pop(0)
        else:
            item = ScriptedResponse(text="{}" if request.json_mode else "ok")

        if isinstance(item, ScriptedError):
            raise item.to_error(provider_request_id=request_id)

        if isinstance(item, str):
            scripted = ScriptedResponse(text=item)
        elif isinstance(item, dict):
            scripted = ScriptedResponse(
                text=json.dumps(item, ensure_ascii=False, sort_keys=True),
                structured_data=item,
            )
        else:
            scripted = item

        usage = scripted.usage or self._estimate_usage(request, scripted.text)
        model = request.model or self.model_id
        return LLMResponse(
            text=scripted.text,
            model_id=model,
            usage=usage,
            provider_request_id=request_id,
            structured_data=scripted.structured_data,
            finish_reason=scripted.finish_reason,
        )

    # ------------------------------------------------------------ internals

    def _estimate_usage(self, request: LLMRequest, text: str) -> LLMUsage:
        prompt_tokens = sum(estimate_tokens(message.content) for message in request.messages)
        return LLMUsage(prompt_tokens=prompt_tokens, completion_tokens=estimate_tokens(text))
