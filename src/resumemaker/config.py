"""Application settings.

Real providers do not exist yet, so a non-local environment that would need them fails loudly at
startup instead of silently running the fake providers in front of real users.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from resumemaker.domain.enums import OperationType
from resumemaker.domain.pricing import INITIAL_GRANT_CREDITS, OPERATION_PRICES

Environment = Literal["local", "test", "staging", "production"]


class ImproperlyConfigured(RuntimeError):
    """The configuration cannot be used safely in the selected environment."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESUMEMAKER_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    env: Environment = "local"
    database_url: str = (
        "postgresql+psycopg://resumemaker:resumemaker@127.0.0.1:5432/resumemaker"
    )

    #: Payments and LLM are fake-only until the integration and capability tests are done.
    payments_provider: Literal["fake"] = "fake"
    llm_provider: Literal["fake"] = "fake"

    #: Signs fake webhooks. Local/test value only; never a production secret.
    fake_payment_secret: str = "local-fake-secret-change-me"
    enable_fake_webhook_endpoint: bool = False

    initial_grant_credits: int = Field(default=INITIAL_GRANT_CREDITS, ge=0)

    @model_validator(mode="after")
    def _reject_fakes_outside_local(self) -> Settings:
        if self.env in {"staging", "production"}:
            raise ImproperlyConfigured(
                "real payment/LLM providers are not implemented; "
                f"environment {self.env!r} cannot run fake providers"
            )
        if self.initial_grant_credits < OPERATION_PRICES[OperationType.PREVIEW]:
            raise ImproperlyConfigured(
                "initial grant must cover at least one preview generation "
                f"({OPERATION_PRICES[OperationType.PREVIEW]} credits)"
            )
        return self

    @property
    def is_local(self) -> bool:
        return self.env in {"local", "test"}
