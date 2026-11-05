"""Engine and session helpers."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from resumemaker.config import Settings


def create_engine_for(settings: Settings, *, echo: bool = False) -> Engine:
    return create_engine(
        settings.database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def unit_of_work(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on any exception.

    Services assume they run inside one transaction; this helper is the only place that decides
    when a transaction ends.
    """

    session = factory()
    try:
        with session.begin():
            yield session
    finally:
        session.close()
