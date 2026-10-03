"""Database bootstrap of a deployment (M19): migrations, roles, Core Banking and seeded cases.

Runs before the API starts on every ``docker compose up`` (the ``migrate`` service) and is
idempotent. In order:

1. Migrations. On a new database the owner role does not exist yet, so the bootstrap superuser
   creates the tables, as in README "Setting up the roles"; afterwards the owner role runs them.
2. Roles (``scripts/sql/roles.sql``): ownership moves to the owner role and the app role gets its
   grants, verified. Re-applying them after every migration covers new tables.
3. Core Banking, as the owner, from the Parquet files of ``data/core``. ``auto`` loads only when
   no real customer is loaded yet (the files are large, and the loader is idempotent anyway);
   ``always`` loads every time; ``never`` skips. Seeded ``SEED-`` rows are never overwritten.
4. The seeded evaluation scenarios (M17), when asked: the customers the demo and the smoke test
   log in with (``SEED-0001`` and the ``TEST_OTP``).

Passwords are never printed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from pathlib import Path

import psycopg
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from app.evaluation.labeler import LABEL_BUSINESS_DATE
from app.evaluation.scenarios import load_scenarios, spec_version
from app.evaluation.seed import SEED_LIKE, build_rows, count_seeded, seed
from app.ingestion.loader import load_core_banking
from app.storage.models import Customer
from app.storage.roles import apply_roles, connect_admin, role_targets

ROOT = Path(__file__).resolve().parents[2]

Log = Callable[[str], None]


class BootstrapError(RuntimeError):
    """The database cannot be prepared; the API must not start on it."""


class CoreLoad(StrEnum):
    AUTO = "auto"
    ALWAYS = "always"
    NEVER = "never"


@dataclass(frozen=True)
class BootstrapConfig:
    admin_url: str = field(repr=False)
    database_url: str = field(repr=False)
    migration_url: str = field(repr=False)
    core_dir: Path
    core_load: CoreLoad = CoreLoad.AUTO
    seed_scenarios: bool = False
    document_hash_key: str = field(default="", repr=False)
    business_date: date | None = None


@dataclass(frozen=True)
class BootstrapReport:
    migrated_as: str  # "admin" on a new database, "owner" afterwards
    core_loaded: bool
    real_customers: int
    seeded: dict[str, int] | None


def alembic_config(url: URL) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "migrations"))
    # configparser interpolates "%": escape it so URL-encoded passwords survive.
    config.set_main_option(
        "sqlalchemy.url", url.render_as_string(hide_password=False).replace("%", "%%")
    )
    config.attributes["configure_logger"] = False
    return config


def _sqlalchemy_url(url: str) -> URL:
    return make_url(url).set(drivername="postgresql+psycopg")


def _libpq_dsn(url: URL) -> str:
    return url.set(drivername="postgresql").render_as_string(hide_password=False)


def role_exists(conn: psycopg.Connection, role: str) -> bool:
    return conn.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is not None


def real_customers(owner_url: URL) -> int:
    """Customers loaded from the dataset (seeded ``SEED-`` customers do not count)."""
    engine = create_engine(owner_url)
    try:
        with Session(engine) as session:
            count = session.scalar(
                select(func.count()).where(Customer.customer_id.not_like(SEED_LIKE))
            )
    finally:
        engine.dispose()
    return int(count or 0)


def _load_core(owner_url: URL, core_dir: Path, log: Log) -> None:
    missing = [name for name in ("customers", "products", "transactions")
               if not (core_dir / f"{name}.parquet").is_file()]  # fmt: skip
    if missing:
        raise BootstrapError(
            f"Core Banking files missing in {core_dir}: {', '.join(missing)}. Build them with "
            "`python scripts/load_core_banking.py --build-only` (same DOCUMENT_HASH_KEY)"
        )
    with psycopg.connect(_libpq_dsn(owner_url)) as connection:
        for load in load_core_banking(connection, core_dir):
            log(
                f"[bootstrap] core {load.table}: file={load.rows_in_file:,} "
                f"before={load.rows_before:,} after={load.rows_after:,}"
            )
        connection.commit()


def _seed(owner_url: URL, config: BootstrapConfig) -> dict[str, int]:
    if config.business_date != LABEL_BUSINESS_DATE:
        raise BootstrapError(
            f"SEED_SCENARIOS needs BUSINESS_DATE={LABEL_BUSINESS_DATE}: the case scripts name "
            "its dates"
        )
    if not config.document_hash_key:
        raise BootstrapError("SEED_SCENARIOS needs DOCUMENT_HASH_KEY")
    rows = build_rows(
        load_scenarios(), config.business_date, config.document_hash_key, spec_version()
    )
    engine = create_engine(owner_url)
    try:
        with Session(engine) as session:
            seed(session, rows)
            session.commit()
            return count_seeded(session)
    finally:
        engine.dispose()


def bootstrap(config: BootstrapConfig, log: Log = print) -> BootstrapReport:
    """Prepare the database for the API. Raises ``BootstrapError`` (or the roles' ``RolesError``)
    when something is missing or wrong."""
    targets = role_targets(config.admin_url, config.database_url, config.migration_url)
    owner_url = _sqlalchemy_url(config.migration_url)

    with connect_admin(targets) as admin:
        new_database = not role_exists(admin, targets.owner_role)
    if new_database:
        log(f"[bootstrap] new database {targets.database}: migrations run as the admin")
        migrated_as = "admin"
        migration_url = _sqlalchemy_url(config.admin_url).set(database=targets.database)
    else:
        migrated_as = "owner"
        migration_url = owner_url
    command.upgrade(alembic_config(migration_url), "head")
    log(f"[bootstrap] migrations at head (as the {migrated_as})")

    with connect_admin(targets) as admin:
        executed = apply_roles(admin, targets.values)
    log(
        f"[bootstrap] roles applied and verified: owner={targets.owner_role} "
        f"app={targets.app_role}, {executed} statements"
    )

    loaded = real_customers(owner_url)
    load = config.core_load is CoreLoad.ALWAYS or (
        config.core_load is CoreLoad.AUTO and loaded == 0
    )
    if load:
        _load_core(owner_url, config.core_dir, log)
        loaded = real_customers(owner_url)
    else:
        log(f"[bootstrap] core load skipped ({config.core_load}): {loaded:,} customers loaded")
    if loaded == 0 and not config.seed_scenarios:
        log("[bootstrap] warning: Core Banking is empty and no scenario is seeded")

    seeded = _seed(owner_url, config) if config.seed_scenarios else None
    if seeded is not None:
        log(f"[bootstrap] seeded scenarios: {seeded}")
    return BootstrapReport(migrated_as, load, loaded, seeded)
