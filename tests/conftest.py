"""Shared fixtures.

Tests run against a real PostgreSQL server: ledger correctness depends on real row locks and
unique constraints, so SQLite would test the wrong thing. Every session creates a throwaway
database, applies the Alembic migrations once, and drops it afterwards.

Set `RESUMEMAKER_TEST_DATABASE_URL` to a database the user may create databases in. The default
matches the disposable cluster started by `scripts/test.ps1`.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool

from resumemaker.db.models import User
from resumemaker.services.ledger import LedgerService

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ADMIN_URL = "postgresql+psycopg://postgres@127.0.0.1:55432/postgres"
TRUNCATE_TABLES = (
    "payment_events",
    "llm_usage",
    "jobs",
    "payments",
    "operations",
    "ledger_entries",
    "consents",
    "users",
)

Uow = Callable[[], AbstractContextManager[Session]]


def admin_url() -> URL:
    return make_url(os.environ.get("RESUMEMAKER_TEST_DATABASE_URL", DEFAULT_ADMIN_URL))


def alembic_config(database_url: str) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@contextmanager
def temporary_database(*, migrate: bool = True) -> Iterator[str]:
    """Create a uniquely named database, optionally migrate it, and always drop it."""

    url = admin_url()
    name = f"rm_test_{uuid.uuid4().hex[:12]}"
    admin_engine = create_engine(url, isolation_level="AUTOCOMMIT", poolclass=NullPool)
    try:
        with admin_engine.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{name}"'))
        database_url = url.set(database=name).render_as_string(hide_password=False)
        if migrate:
            command.upgrade(alembic_config(database_url), "head")
        yield database_url
    finally:
        with admin_engine.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def database_url() -> Iterator[str]:
    try:
        with temporary_database() as url:
            yield url
    except Exception as exc:  # pragma: no cover - environment setup failure
        pytest.exit(
            "PostgreSQL is not reachable for tests "
            f"({exc}). Run .\\scripts\\test.ps1 or set RESUMEMAKER_TEST_DATABASE_URL.",
            returncode=3,
        )


@pytest.fixture(scope="session")
def engine(database_url: str) -> Iterator[Engine]:
    engine = create_engine(database_url, poolclass=NullPool)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture(scope="session")
def session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False)


@pytest.fixture(autouse=True)
def clean_tables(engine: Engine) -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(text(f"TRUNCATE TABLE {', '.join(TRUNCATE_TABLES)} CASCADE"))
    yield


@pytest.fixture
def uow(session_factory: sessionmaker[Session]) -> Uow:
    """Open a committed unit of work, mirroring how production code uses sessions."""

    @contextmanager
    def _open() -> Iterator[Session]:
        session = session_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    return _open


@pytest.fixture
def make_user(uow: Uow) -> Callable[..., uuid.UUID]:
    def _make(
        *,
        credits: int = 100,
        telegram_user_id: int | None = None,
    ) -> uuid.UUID:
        with uow() as session:
            user = User(telegram_user_id=telegram_user_id)
            session.add(user)
            session.flush()
            if credits:
                LedgerService(session).grant_initial(user.id, amount=credits)
            return user.id

    return _make
