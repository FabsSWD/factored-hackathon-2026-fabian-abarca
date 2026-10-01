"""Shared fixtures: the synthetic dataset pipeline and a temporary PostgreSQL database.

Database tests need ``TEST_DATABASE_URL`` (a maintenance database on a PostgreSQL 16 server,
e.g. ``postgresql+psycopg://user:pass@localhost:5432/postgres``). Each run creates
``disputes_test_<uuid>``, migrates it with ``alembic upgrade head``, and drops it at the end,
even when tests fail. Each test runs inside a transaction that is rolled back.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import time
from pathlib import Path

import psycopg
import pytest
from alembic import command
from alembic.config import Config
from dotenv import dotenv_values
from sqlalchemy import Connection, Engine, create_engine
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.ingestion.core import CoreBuildConfig, CoreBuildReport, build_core
from app.ingestion.loader import load_core_banking
from tests.fixtures.core_banking import BUSINESS_DATE, write_raw_dataset

ROOT = Path(__file__).resolve().parent.parent
TEST_HASH_KEY = "test-document-hash-key"


# ---------------------------------------------------------------------------
# Synthetic dataset through the real pipeline
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Pipeline:
    root: Path
    raw_dir: Path
    silver_dir: Path
    core_dir: Path
    report: CoreBuildReport


def core_config(root: Path, **overrides: object) -> CoreBuildConfig:
    values: dict[str, object] = {
        "silver_dir": root / "data" / "silver",
        "exchange_rates_csv": root / "data" / "raw" / "daily_exchange_rates.csv",
        "core_dir": root / "data" / "core",
        "business_date": BUSINESS_DATE,
        "business_day_cutoff": time(6, 0),
        "fx_max_staleness_days": 3,
        "document_hash_key": TEST_HASH_KEY,
    }
    values.update(overrides)
    return CoreBuildConfig(**values)  # type: ignore[arg-type]


def run_ingest(root: Path, *tables: str) -> subprocess.CompletedProcess[str]:
    """Run scripts/ingest.py as the pipeline does, with ``root`` as working directory."""
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "ingest.py"), *tables],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


@pytest.fixture(scope="session")
def pipeline(tmp_path_factory: pytest.TempPathFactory) -> Pipeline:
    root = tmp_path_factory.mktemp("dataset")
    raw = write_raw_dataset(root)
    result = run_ingest(root, "customers", "products", "transactions")
    assert result.returncode == 0, result.stderr
    report = build_core(core_config(root))
    return Pipeline(root, raw, root / "data" / "silver", root / "data" / "core", report)


# ---------------------------------------------------------------------------
# Temporary PostgreSQL database
# ---------------------------------------------------------------------------


def _test_server_url() -> URL:
    value = os.environ.get("TEST_DATABASE_URL") or dotenv_values(ROOT / ".env").get(
        "TEST_DATABASE_URL"
    )
    if not value:
        pytest.fail(
            "TEST_DATABASE_URL is not set: database tests need a PostgreSQL 16 server "
            "(see tests/conftest.py)",
            pytrace=False,
        )
    return make_url(value)


def _admin_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def alembic_config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # configparser interpolates "%": escape it so URL-encoded passwords survive.
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    config.attributes["configure_logger"] = False
    return config


@pytest.fixture(scope="session")
def database_url() -> Iterator[URL]:
    server = _test_server_url()
    name = f"disputes_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = server.set(database=name)
    try:
        command.upgrade(alembic_config(url), "head")
        yield url
    finally:
        with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


@pytest.fixture(scope="session")
def engine(database_url: URL) -> Iterator[Engine]:
    engine = create_engine(database_url)
    yield engine
    engine.dispose()


@pytest.fixture
def connection(engine: Engine) -> Iterator[Connection]:
    """A connection inside a transaction that is always rolled back."""
    with engine.connect() as conn:
        transaction = conn.begin()
        try:
            yield conn
        finally:
            transaction.rollback()


@pytest.fixture
def db_session(connection: Connection) -> Iterator[Session]:
    session = Session(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()


@pytest.fixture
def raw_psycopg(connection: Connection) -> psycopg.Connection:
    """The psycopg connection under ``connection``, sharing its transaction."""
    driver = connection.connection.driver_connection
    assert isinstance(driver, psycopg.Connection)
    return driver


@pytest.fixture
def loaded(pipeline: Pipeline, raw_psycopg: psycopg.Connection) -> Pipeline:
    """The synthetic dataset loaded into the test database (rolled back after the test)."""
    load_core_banking(raw_psycopg, pipeline.core_dir)
    return pipeline
