"""Consents are separate, versioned and append-only."""

from __future__ import annotations

import hashlib

import pytest

from resumemaker.domain.enums import ConsentKind
from resumemaker.domain.errors import NotFound
from resumemaker.services.consents import ConsentService

TEXT = "Я согласен(на) на обработку данных для подготовки резюме."


def test_consent_records_version_and_text_hash(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with uow() as session:
        consent = ConsentService(session).record(
            user_id=user_id,
            kind=ConsentKind.DATA_PROCESSING,
            granted=True,
            document_version="2026-11-01",
            document_text=TEXT,
            source="bot_button",
        )
        assert consent.document_version == "2026-11-01"
        assert consent.document_sha256 == hashlib.sha256(TEXT.encode()).hexdigest()
        assert consent.granted is True


def test_both_consents_are_required_for_paid_work(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with uow() as session:
        service = ConsentService(session)
        service.record(
            user_id=user_id,
            kind=ConsentKind.DATA_PROCESSING,
            granted=True,
            document_version="v1",
            document_text=TEXT,
            source="bot_button",
        )
        assert service.has_granted(user_id, ConsentKind.DATA_PROCESSING)
        assert not service.has_granted(user_id, ConsentKind.EXTERNAL_LLM)

        with pytest.raises(NotFound) as error:
            service.require_both(user_id)
        assert "external_llm" in str(error.value)


def test_latest_consent_wins(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with uow() as session:
        service = ConsentService(session)
        service.record(
            user_id=user_id,
            kind=ConsentKind.EXTERNAL_LLM,
            granted=True,
            document_version="v1",
            document_text=TEXT,
            source="bot_button",
        )
    with uow() as session:
        service = ConsentService(session)
        service.record(
            user_id=user_id,
            kind=ConsentKind.EXTERNAL_LLM,
            granted=False,
            document_version="v1",
            document_text=TEXT,
            source="bot_command",
        )

    with uow() as session:
        service = ConsentService(session)
        assert service.has_granted(user_id, ConsentKind.EXTERNAL_LLM) is False
        with pytest.raises(NotFound):
            service.require_both(user_id)


def test_both_consents_together_satisfy_the_gate(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with uow() as session:
        service = ConsentService(session)
        for kind in (ConsentKind.DATA_PROCESSING, ConsentKind.EXTERNAL_LLM):
            service.record(
                user_id=user_id,
                kind=kind,
                granted=True,
                document_version="v1",
                document_text=TEXT,
                source="bot_button",
            )
        service.require_both(user_id)


def test_unknown_source_is_refused(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with pytest.raises(ValueError):
        with uow() as session:
            ConsentService(session).record(
                user_id=user_id,
                kind=ConsentKind.DATA_PROCESSING,
                granted=True,
                document_version="v1",
                document_text=TEXT,
                source="scraped",
            )


def test_empty_document_version_is_refused(uow, make_user) -> None:
    user_id = make_user(credits=0)

    with pytest.raises(ValueError):
        with uow() as session:
            ConsentService(session).record(
                user_id=user_id,
                kind=ConsentKind.DATA_PROCESSING,
                granted=True,
                document_version="  ",
                document_text=TEXT,
                source="api",
            )
