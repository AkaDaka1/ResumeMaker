"""Consent service.

Data-processing consent and external-LLM-transfer consent are recorded as separate events with
the document version and a hash of the exact text the user saw. History is append-only.
"""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from resumemaker.db.models import Consent
from resumemaker.domain.enums import ConsentKind
from resumemaker.domain.errors import NotFound

_SOURCES = {"bot_button", "bot_command", "api", "admin_import"}


def document_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class ConsentService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def record(
        self,
        *,
        user_id: uuid.UUID,
        kind: ConsentKind,
        granted: bool,
        document_version: str,
        document_text: str,
        source: str,
    ) -> Consent:
        if not document_version.strip():
            raise ValueError("document_version is required")
        if source not in _SOURCES:
            raise ValueError(f"unsupported consent source: {source}")
        consent = Consent(
            user_id=user_id,
            kind=kind,
            granted=granted,
            document_version=document_version,
            document_sha256=document_hash(document_text),
            source=source,
        )
        self.session.add(consent)
        self.session.flush()
        return consent

    def latest(self, user_id: uuid.UUID, kind: ConsentKind) -> Consent | None:
        return self.session.scalar(
            select(Consent)
            .where(Consent.user_id == user_id, Consent.kind == kind)
            .order_by(Consent.created_at.desc(), Consent.id.desc())
            .limit(1)
        )

    def has_granted(self, user_id: uuid.UUID, kind: ConsentKind) -> bool:
        latest = self.latest(user_id, kind)
        return latest is not None and latest.granted

    def require_both(self, user_id: uuid.UUID) -> None:
        """Raise when either consent is missing or was withdrawn.

        The operation layer calls this before anything paid starts.
        """

        missing = [
            kind
            for kind in (ConsentKind.DATA_PROCESSING, ConsentKind.EXTERNAL_LLM)
            if not self.has_granted(user_id, kind)
        ]
        if missing:
            raise NotFound(
                "missing consent(s): " + ", ".join(kind.value for kind in missing)
            )
