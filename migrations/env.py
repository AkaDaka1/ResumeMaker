"""Alembic environment.

The database URL is never hardcoded in `alembic.ini`: it comes from settings or from an explicit
override (tests set `sqlalchemy.url` on the Config object before calling `upgrade`).
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine, pool

import resumemaker.db.models  # noqa: F401  (registers tables on the metadata)
from resumemaker.config import Settings
from resumemaker.db.base import Base

config = context.config
target_metadata = Base.metadata


def _database_url() -> str:
    configured = config.get_main_option("sqlalchemy.url")
    if configured:
        return configured
    return Settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_database_url(), poolclass=pool.NullPool, future=True)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
