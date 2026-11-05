"""Fake payment provider for local development and tests.

It behaves like a real acquirer in the ways that matter for the application:

* creating a payment returns a provider id, a one-off URL and an expiry;
* webhooks are signed with HMAC-SHA256 over the raw body and verified in constant time;
* a webhook carries amount, currency, purpose and operation id, so a mismatch is detectable;
* the provider never confirms anything on the client side.

It performs no network calls and holds state in memory only.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from resumemaker.domain.enums import PaymentStatus
from resumemaker.domain.errors import PaymentVerificationError
from resumemaker.providers.payments.base import (
    PaymentSessionRequest,
    PaymentSessionResult,
    ProviderPaymentEvent,
)

_SIGNATURE_HEADER = "x-fake-signature"


@dataclass
class _FakeSession:
    provider_payment_id: str
    payment_id: uuid.UUID
    amount_credits: int
    currency: str
    purpose: str
    operation_id: uuid.UUID | None
    expires_at: dt.datetime
    payment_url: str
    status: PaymentStatus


class FakePaymentProvider:
    name = "fake"
    signature_header = "X-Fake-Signature"

    def __init__(
        self,
        *,
        secret: str,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        if not secret:
            raise ValueError("fake payment secret must not be empty")
        self._secret = secret.encode("utf-8")
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._sessions: dict[str, _FakeSession] = {}

    # ------------------------------------------------------------- protocol

    def create_session(self, request: PaymentSessionRequest) -> PaymentSessionResult:
        if request.amount_credits <= 0:
            raise ValueError("amount_credits must be positive")
        if request.ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        provider_payment_id = f"fakepay_{uuid.uuid4().hex}"
        expires_at = self._clock() + dt.timedelta(seconds=request.ttl_seconds)
        self._sessions[provider_payment_id] = _FakeSession(
            provider_payment_id=provider_payment_id,
            payment_id=request.payment_id,
            amount_credits=request.amount_credits,
            currency=request.currency,
            purpose=request.purpose,
            operation_id=request.operation_id,
            expires_at=expires_at,
            payment_url=f"https://fake-payments.local/session/{provider_payment_id}",
            status=PaymentStatus.PENDING,
        )
        return PaymentSessionResult(
            provider_payment_id=provider_payment_id,
            payment_url=self._sessions[provider_payment_id].payment_url,
            expires_at=expires_at,
        )

    def verify_webhook(
        self, body: bytes, headers: Mapping[str, str]
    ) -> ProviderPaymentEvent:
        signature = _header(headers, _SIGNATURE_HEADER)
        if not signature:
            raise PaymentVerificationError("missing webhook signature")
        expected = self._sign(body)
        if not hmac.compare_digest(signature, expected):
            raise PaymentVerificationError("invalid webhook signature")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise PaymentVerificationError("webhook body is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise PaymentVerificationError("webhook body must be an object")

        event_id = payload.get("event_id")
        event_type = payload.get("event_type")
        payment = payload.get("payment")
        if not isinstance(event_id, str) or not event_id:
            raise PaymentVerificationError("webhook is missing event_id")
        if not isinstance(event_type, str) or not event_type:
            raise PaymentVerificationError("webhook is missing event_type")
        if not isinstance(payment, dict):
            raise PaymentVerificationError("webhook is missing payment object")

        provider_payment_id = payment.get("provider_payment_id")
        status_raw = payment.get("status")
        amount_credits = payment.get("amount_credits")
        currency = payment.get("currency")
        purpose = payment.get("purpose")
        operation_id_raw = payment.get("operation_id")

        if not isinstance(provider_payment_id, str) or not provider_payment_id:
            raise PaymentVerificationError("webhook is missing provider_payment_id")
        if not isinstance(status_raw, str):
            raise PaymentVerificationError("webhook is missing payment status")
        try:
            status = PaymentStatus(status_raw)
        except ValueError as exc:
            raise PaymentVerificationError(f"unknown payment status: {status_raw!r}") from exc
        if not isinstance(amount_credits, int) or isinstance(amount_credits, bool):
            raise PaymentVerificationError("amount_credits must be an integer")
        if amount_credits <= 0:
            raise PaymentVerificationError("amount_credits must be positive")
        if not isinstance(currency, str) or not currency:
            raise PaymentVerificationError("webhook is missing currency")
        if not isinstance(purpose, str) or not purpose:
            raise PaymentVerificationError("webhook is missing purpose")
        if operation_id_raw is not None:
            if not isinstance(operation_id_raw, str):
                raise PaymentVerificationError("operation_id must be a string or null")
            try:
                operation_id = uuid.UUID(operation_id_raw)
            except ValueError as exc:
                raise PaymentVerificationError("operation_id is not a valid UUID") from exc
        else:
            operation_id = None

        return ProviderPaymentEvent(
            provider=self.name,
            event_id=event_id,
            event_type=event_type,
            provider_payment_id=provider_payment_id,
            status=status,
            amount_credits=amount_credits,
            currency=currency,
            purpose=purpose,
            operation_id=operation_id,
            payload_sha256=hashlib.sha256(body).hexdigest(),
        )

    def get_status(self, provider_payment_id: str) -> ProviderPaymentEvent:
        session = self._sessions.get(provider_payment_id)
        if session is None:
            raise PaymentVerificationError(f"unknown provider payment: {provider_payment_id}")
        return ProviderPaymentEvent(
            provider=self.name,
            event_id=f"status_{provider_payment_id}_{session.status.value}",
            event_type="payment.status",
            provider_payment_id=provider_payment_id,
            status=session.status,
            amount_credits=session.amount_credits,
            currency=session.currency,
            purpose=session.purpose,
            operation_id=session.operation_id,
            payload_sha256=hashlib.sha256(
                f"{provider_payment_id}:{session.status.value}".encode()
            ).hexdigest(),
        )

    # --------------------------------------------------------- test helpers

    def mark_paid(self, provider_payment_id: str) -> None:
        self._require_session(provider_payment_id).status = PaymentStatus.PAID

    def mark_status(self, provider_payment_id: str, status: PaymentStatus) -> None:
        self._require_session(provider_payment_id).status = status

    def build_webhook(
        self,
        provider_payment_id: str,
        *,
        status: PaymentStatus = PaymentStatus.PAID,
        event_id: str | None = None,
        event_type: str = "payment.updated",
        amount_credits: int | None = None,
        currency: str | None = None,
        purpose: str | None = None,
        operation_id: uuid.UUID | None = None,
    ) -> tuple[bytes, dict[str, str]]:
        """Build a correctly signed webhook, optionally with wrong values for negative tests."""

        session = self._sessions.get(provider_payment_id)
        if session is not None:
            defaults = {
                "amount_credits": session.amount_credits,
                "currency": session.currency,
                "purpose": session.purpose,
                "operation_id": session.operation_id,
            }
            session.status = status
        else:
            defaults = {
                "amount_credits": None,
                "currency": None,
                "purpose": None,
                "operation_id": None,
            }

        resolved_amount = (
            amount_credits if amount_credits is not None else defaults["amount_credits"]
        )
        resolved_currency = currency if currency is not None else defaults["currency"]
        resolved_purpose = purpose if purpose is not None else defaults["purpose"]
        resolved_operation = (
            operation_id if operation_id is not None else defaults["operation_id"]
        )
        if resolved_amount is None or resolved_currency is None or resolved_purpose is None:
            raise ValueError(
                "unknown provider payment: amount_credits, currency and purpose are required"
            )

        payload = {
            "event_id": event_id or f"evt_{uuid.uuid4().hex}",
            "event_type": event_type,
            "payment": {
                "provider_payment_id": provider_payment_id,
                "status": status.value,
                "amount_credits": resolved_amount,
                "currency": resolved_currency,
                "purpose": resolved_purpose,
                "operation_id": str(resolved_operation) if resolved_operation else None,
            },
        }
        body = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
        return body, {self.signature_header: self._sign(body)}

    def sessions(self) -> dict[str, _FakeSession]:
        return dict(self._sessions)

    # ------------------------------------------------------------- internals

    def _sign(self, body: bytes) -> str:
        return hmac.new(self._secret, body, hashlib.sha256).hexdigest()

    def _require_session(self, provider_payment_id: str) -> _FakeSession:
        try:
            return self._sessions[provider_payment_id]
        except KeyError as exc:
            raise PaymentVerificationError(
                f"unknown provider payment: {provider_payment_id}"
            ) from exc


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name:
            return value
    return None
