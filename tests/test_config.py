"""Configuration guards: no fake providers behind a production-looking environment."""

from __future__ import annotations

import pytest

from resumemaker.config import ImproperlyConfigured, Settings
from resumemaker.domain.enums import OperationType
from resumemaker.domain.pricing import OPERATION_PRICES


def test_local_defaults_are_safe() -> None:
    settings = Settings(env="local")

    assert settings.payments_provider == "fake"
    assert settings.llm_provider == "fake"
    assert settings.enable_fake_webhook_endpoint is False
    assert settings.is_local


def test_production_is_refused_while_only_fakes_exist() -> None:
    with pytest.raises(ImproperlyConfigured):
        Settings(env="production")


def test_staging_is_refused_while_only_fakes_exist() -> None:
    with pytest.raises(ImproperlyConfigured):
        Settings(env="staging")


def test_initial_grant_must_cover_preview() -> None:
    with pytest.raises(ImproperlyConfigured):
        Settings(env="local", initial_grant_credits=OPERATION_PRICES[OperationType.PREVIEW] - 1)


def test_database_url_comes_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    url = "postgresql+psycopg://user:pass@db.example:5432/resumemaker"
    monkeypatch.setenv("RESUMEMAKER_DATABASE_URL", url)

    assert Settings().database_url == url
