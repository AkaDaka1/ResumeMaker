"""The initial migration must apply, match the models and roll back cleanly."""

from __future__ import annotations

import datetime as dt
import uuid

from alembic import command
from sqlalchemy import create_engine, inspect, text

from resumemaker.db.base import Base
from tests.conftest import alembic_config, temporary_database


def test_upgrade_matches_models_and_downgrade_is_clean() -> None:
    with temporary_database(migrate=False) as url:
        config = alembic_config(url)
        command.upgrade(config, "head")

        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert set(Base.metadata.tables) <= tables
            assert "alembic_version" in tables

            constraints = set(
                engine.connect()
                .execute(
                    text(
                        "SELECT conname FROM pg_constraint "
                        "WHERE conrelid = 'ledger_entries'::regclass"
                    )
                )
                .scalars()
            )
            assert "uq_ledger_entries_idempotency_key" in constraints
            assert "ck_ledger_entries_amount_not_zero" in constraints

            command.downgrade(config, "base")
            assert set(inspect(engine).get_table_names()) == {"alembic_version"}

            command.upgrade(config, "head")
            assert set(Base.metadata.tables) <= set(inspect(engine).get_table_names())
        finally:
            engine.dispose()


def test_server_defaults_and_enum_values_are_lowercase() -> None:
    with temporary_database() as url:
        engine = create_engine(url)
        user_id = uuid.uuid4()
        try:
            with engine.begin() as connection:
                connection.execute(
                    text("INSERT INTO users (id) VALUES (:id)"), {"id": user_id}
                )
                row = connection.execute(
                    text(
                        "SELECT interview_language, created_at "
                        "FROM users WHERE id = :id"
                    ),
                    {"id": user_id},
                ).one()
            assert row[0] == "ru"
            assert isinstance(row[1], dt.datetime)
        finally:
            engine.dispose()
