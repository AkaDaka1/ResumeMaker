"""Payment provider contract.

The application owns `payment_id`; the provider owns `provider_payment_id`. Nothing outside this
protocol may know a bank's or Telegram's payload shape.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from resumemaker.domain.enums import PaymentStatus


@dataclass(frozen=True)
class PaymentSessionRequest:
    payment_id: uuid.UUID
    amount_credits: int
    currency: str
    purpose: str
    description: str | None = None
    operation_id: uuid.UUID | None = None
    ttl_seconds: int = 900


@dataclass(frozen=True)
class PaymentSessionResult:
    provider_payment_id: str
    payment_url: str | None
    expires_at: dt.datetime


@dataclass(frozen=True)
class ProviderPaymentEvent:
    """A verified, trustworthy provider event.

    Constructed only after signature verification; the payment service then reconciles it with
    the locally stored payment before changing any state.
    """

    provider: str
    event_id: str
    event_type: str
    provider_payment_id: str
    status: PaymentStatus
    amount_credits: int
    currency: str
    purpose: str
    operation_id: uuid.UUID | None
    payload_sha256: str


class PaymentProvider(Protocol):
    name: str

    def create_session(self, request: PaymentSessionRequest) -> PaymentSessionResult: ...

    def verify_webhook(
        self, body: bytes, headers: Mapping[str, str]
    ) -> ProviderPaymentEvent: ...

    def get_status(self, provider_payment_id: str) -> ProviderPaymentEvent: ...
