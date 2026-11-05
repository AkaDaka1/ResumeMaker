"""HTTP shell: health endpoints and the local-only fake payment webhook."""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from resumemaker.api.app import create_app
from resumemaker.config import Settings
from resumemaker.db.session import unit_of_work
from resumemaker.domain.enums import PaymentMethod
from resumemaker.services.ledger import LedgerService
from resumemaker.services.payments import PaymentService


@pytest.fixture
def settings(database_url: str) -> Settings:
    return Settings(
        env="test",
        database_url=database_url,
        fake_payment_secret="api-test-secret",
        enable_fake_webhook_endpoint=True,
    )


def test_healthz(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/healthz")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_readyz_checks_the_database(settings: Settings) -> None:
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_fake_webhook_grants_credits(settings: Settings, make_user) -> None:
    app = create_app(settings)
    provider = app.state.payment_provider
    user_id = make_user(credits=0)

    with unit_of_work(app.state.session_factory) as session:
        payment = PaymentService(session, provider).create(
            user_id=user_id,
            method=PaymentMethod.TELEGRAM_STARS,
            amount_credits=100,
            idempotency_key=f"api-{uuid.uuid4()}",
        )
        provider_payment_id = payment.provider_payment_id
    assert provider_payment_id is not None

    body, headers = provider.build_webhook(provider_payment_id)

    with TestClient(app) as client:
        response = client.post(
            "/payments/fake/webhook", content=body, headers=headers
        )

    assert response.status_code == 200
    assert response.json()["outcome"] == "applied"

    with unit_of_work(app.state.session_factory) as session:
        assert LedgerService(session).balance(user_id) == 100


def test_fake_webhook_rejects_bad_signature(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.post(
            "/payments/fake/webhook",
            content=b'{"event_id": "x"}',
            headers={"X-Fake-Signature": "0" * 64},
        )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid webhook"


def test_fake_webhook_is_hidden_when_disabled(database_url: str) -> None:
    settings = Settings(
        env="test",
        database_url=database_url,
        enable_fake_webhook_endpoint=False,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post("/payments/fake/webhook", content=b"{}")

    assert response.status_code == 404
