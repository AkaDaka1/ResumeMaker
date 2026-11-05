"""FastAPI application.

Sprint 1 exposes health/readiness plus a clearly separated fake-payment webhook that only exists
in local/test mode. It contains no Telegram integration and no real provider client.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from resumemaker import __version__
from resumemaker.config import Settings
from resumemaker.db.session import create_engine_for, create_session_factory, unit_of_work
from resumemaker.domain.errors import PaymentVerificationError
from resumemaker.providers.payments.fake import FakePaymentProvider
from resumemaker.services.payments import PaymentService


def get_session(request: Request) -> Iterator[Session]:
    """Request-scoped unit of work; module level so FastAPI can resolve the annotation."""

    with unit_of_work(request.app.state.session_factory) as session:
        yield session


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings()
    engine = create_engine_for(settings)
    session_factory = create_session_factory(engine)
    payment_provider = FakePaymentProvider(secret=settings.fake_payment_secret)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="ResumeMaker",
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs" if settings.is_local else None,
        redoc_url=None,
    )
    app.state.settings = settings
    app.state.engine = engine
    app.state.session_factory = session_factory
    app.state.payment_provider = payment_provider

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/readyz")
    def readyz(request: Request, response: Response) -> dict[str, str]:
        try:
            with request.app.state.engine.connect() as connection:
                connection.execute(text("SELECT 1"))
        except SQLAlchemyError:
            response.status_code = 503
            return {"status": "unavailable"}
        return {"status": "ready"}

    if settings.is_local and settings.enable_fake_webhook_endpoint:

        @app.post("/payments/fake/webhook")
        async def fake_payment_webhook(
            request: Request,
            session: Annotated[Session, Depends(get_session)],
        ) -> dict[str, str | None]:
            body = await request.body()
            service = PaymentService(session, request.app.state.payment_provider)
            try:
                result = service.handle_webhook(body, dict(request.headers))
            except PaymentVerificationError:
                # Never echo the underlying reason: it can hint at signature construction.
                raise HTTPException(status_code=400, detail="invalid webhook") from None
            return {
                "outcome": result.outcome.value,
                "payment_id": str(result.payment_id) if result.payment_id else None,
                "reason": result.reason,
            }

    return app
