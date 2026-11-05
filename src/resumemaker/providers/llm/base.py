"""LLM provider contract.

The adapter shape follows the product decision:

    generate(request, model, schema?)
        -> response(text, structured_data?, usage, provider_request_id)

Provider-specific payloads stay inside the adapter. Errors are classified with the same taxonomy
as AnyModel so retry policy is written once, in the application.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol


class LLMMessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"


@dataclass(frozen=True)
class LLMMessage:
    role: LLMMessageRole
    content: str


@dataclass(frozen=True)
class LLMRequest:
    model: str
    messages: tuple[LLMMessage, ...]
    temperature: float | None = None
    max_tokens: int | None = None
    reasoning_effort: str | None = None
    #: Ask for a bare JSON document. Structured output is *not* assumed to be supported; the
    #: caller still parses and validates strictly.
    json_mode: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LLMUsage:
    prompt_tokens: int
    completion_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens


@dataclass(frozen=True)
class LLMResponse:
    text: str
    model_id: str
    usage: LLMUsage
    provider_request_id: str | None
    structured_data: dict[str, Any] | None = None
    finish_reason: str | None = None


class LLMProvider(Protocol):
    name: str

    def generate(self, request: LLMRequest) -> LLMResponse: ...
