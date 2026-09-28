from __future__ import annotations

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, inspect
from sqlalchemy.engine import URL

from app.storage.models import Base
from tests.conftest import alembic_config

TABLES = {
    "alembic_version",
    "audit_logs",
    "cases",
    "customers",
    "handoff_packets",
    "products",
    "sessions",
    "transactions",
}


def test_upgrade_creates_every_table(engine: Engine) -> None:
    assert set(inspect(engine).get_table_names()) == TABLES


def test_migrations_match_the_models(engine: Engine) -> None:
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        assert compare_metadata(context, Base.metadata) == []


def test_downgrade_to_base_and_upgrade_again(database_url: URL, engine: Engine) -> None:
    config = alembic_config(database_url)
    command.downgrade(config, "base")
    engine.dispose()
    assert set(inspect(engine).get_table_names()) == {"alembic_version"}
    command.upgrade(config, "head")
    engine.dispose()
    assert set(inspect(engine).get_table_names()) == TABLES
