"""Database bootstrap of a deployment (app/storage/bootstrap.py, scripts/bootstrap_db.py).

Reproduces the `migrate` service on a new database: the container's entrypoint creates it, owned
by the bootstrap superuser and empty. The first run migrates as the admin, creates the roles and
loads Core Banking; later runs migrate as the owner and leave the loaded data alone.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path

import psycopg
import pytest

from app.storage.bootstrap import (
    BootstrapConfig,
    BootstrapError,
    BootstrapReport,
    CoreLoad,
    bootstrap,
)
from app.storage.roles import RolesError
from tests.conftest import ROOT, TEST_HASH_KEY, Pipeline, _admin_dsn, _test_server_url
from tests.fixtures.core_banking import BUSINESS_DATE

OWNER_PASSWORD = "owner-pass"
APP_PASSWORD = "app-pass"


@dataclass(frozen=True)
class FreshDb:
    admin_url: str  # the bootstrap superuser on the maintenance database
    database_url: str
    migration_url: str
    target_dsn: str  # the bootstrap superuser on the new database
    owner: str
    app: str

    def config(self, core_dir: Path, **overrides: object) -> BootstrapConfig:
        config = BootstrapConfig(
            admin_url=self.admin_url,
            database_url=self.database_url,
            migration_url=self.migration_url,
            core_dir=core_dir,
            document_hash_key=TEST_HASH_KEY,
            business_date=BUSINESS_DATE,
        )
        return replace(config, **overrides)  # type: ignore[arg-type]

    def scalar(self, sql: str, *params: object) -> object:
        with psycopg.connect(self.target_dsn) as conn:
            row = conn.execute(sql, params or None).fetchone()
        assert row is not None
        return row[0]


@pytest.fixture(scope="module")
def fresh_db() -> Iterator[FreshDb]:
    server = _test_server_url()
    suffix = uuid.uuid4().hex[:10]
    name, owner, app = f"disputes_boot_{suffix}", f"b_owner_{suffix}", f"b_app_{suffix}"
    with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    target = server.set(database=name)

    def role_url(role: str, password: str) -> str:
        return target.set(username=role, password=password).render_as_string(hide_password=False)

    try:
        yield FreshDb(
            admin_url=server.render_as_string(hide_password=False),
            database_url=role_url(app, APP_PASSWORD),
            migration_url=role_url(owner, OWNER_PASSWORD),
            target_dsn=_admin_dsn(target),
            owner=owner,
            app=app,
        )
    finally:
        with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            for role in (owner, app):
                admin.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture(scope="module")
def first_run(fresh_db: FreshDb, pipeline: Pipeline) -> BootstrapReport:
    lines: list[str] = []
    report = bootstrap(fresh_db.config(pipeline.core_dir, seed_scenarios=True), lines.append)
    assert not any(OWNER_PASSWORD in line or APP_PASSWORD in line for line in lines)
    return report


def test_a_new_database_is_migrated_by_the_admin(first_run: BootstrapReport) -> None:
    assert first_run.migrated_as == "admin"


def test_the_owner_owns_every_table_afterwards(
    first_run: BootstrapReport, fresh_db: FreshDb
) -> None:
    owners = fresh_db.scalar(
        "SELECT array_agg(DISTINCT tableowner) FROM pg_tables WHERE schemaname = 'public'"
    )
    assert owners == [fresh_db.owner]


def test_core_banking_is_loaded_from_the_files(
    first_run: BootstrapReport, fresh_db: FreshDb, pipeline: Pipeline
) -> None:
    assert first_run.core_loaded
    assert first_run.real_customers > 0
    loaded = fresh_db.scalar("SELECT count(*) FROM customers WHERE customer_id NOT LIKE 'SEED-%'")
    assert loaded == first_run.real_customers


def test_scenarios_are_seeded(first_run: BootstrapReport) -> None:
    assert first_run.seeded is not None
    assert first_run.seeded["customers"] > 0


def test_the_app_role_logs_in_and_reads_core_banking(
    first_run: BootstrapReport, fresh_db: FreshDb
) -> None:
    dsn = fresh_db.database_url.replace("+psycopg", "")
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT count(*) FROM customers").fetchone()
        assert row is not None and row[0] > 0
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("DELETE FROM customers")


def test_a_second_run_migrates_as_the_owner_and_skips_the_load(
    first_run: BootstrapReport, fresh_db: FreshDb, pipeline: Pipeline
) -> None:
    lines: list[str] = []
    report = bootstrap(fresh_db.config(pipeline.core_dir), lines.append)
    assert report.migrated_as == "owner"
    assert not report.core_loaded
    assert report.real_customers == first_run.real_customers
    assert report.seeded is None
    assert any("core load skipped (auto)" in line for line in lines)


def test_core_load_always_reloads_idempotently(
    first_run: BootstrapReport, fresh_db: FreshDb, pipeline: Pipeline
) -> None:
    report = bootstrap(fresh_db.config(pipeline.core_dir, core_load=CoreLoad.ALWAYS), print)
    assert report.core_loaded
    assert report.real_customers == first_run.real_customers


def test_missing_core_files_are_an_error_when_a_load_is_due(
    first_run: BootstrapReport, fresh_db: FreshDb, tmp_path: Path
) -> None:
    config = fresh_db.config(tmp_path, core_load=CoreLoad.ALWAYS)
    with pytest.raises(BootstrapError, match="Core Banking files missing"):
        bootstrap(config, print)


def test_core_load_never_does_not_need_the_files(
    first_run: BootstrapReport, fresh_db: FreshDb, tmp_path: Path
) -> None:
    report = bootstrap(fresh_db.config(tmp_path, core_load=CoreLoad.NEVER), print)
    assert not report.core_loaded


def test_seeding_needs_the_label_business_date_and_the_key(
    first_run: BootstrapReport, fresh_db: FreshDb, pipeline: Pipeline
) -> None:
    wrong_date = fresh_db.config(
        pipeline.core_dir, seed_scenarios=True, business_date=BUSINESS_DATE.replace(day=1)
    )
    with pytest.raises(BootstrapError, match="BUSINESS_DATE"):
        bootstrap(wrong_date, print)
    no_key = fresh_db.config(pipeline.core_dir, seed_scenarios=True, document_hash_key="")
    with pytest.raises(BootstrapError, match="DOCUMENT_HASH_KEY"):
        bootstrap(no_key, print)


def test_the_admin_as_owner_is_refused(fresh_db: FreshDb, pipeline: Pipeline) -> None:
    config = fresh_db.config(pipeline.core_dir, migration_url=fresh_db.admin_url)
    with pytest.raises(RolesError):
        bootstrap(config, print)


# --- scripts/bootstrap_db.py ---------------------------------------------------------------


def run_script(fresh_db: FreshDb, **env: str) -> subprocess.CompletedProcess[str]:
    environment = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "ADMIN_DATABASE_URL": fresh_db.admin_url,
        "DATABASE_URL": fresh_db.database_url,
        "MIGRATION_DATABASE_URL": fresh_db.migration_url,
        "BUSINESS_DATE": BUSINESS_DATE.isoformat(),
        "CORE_LOAD": "never",
        "SEED_SCENARIOS": "false",
        **env,
    }
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "bootstrap_db.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
        check=False,
    )


def test_script_runs_end_to_end(first_run: BootstrapReport, fresh_db: FreshDb) -> None:
    result = run_script(fresh_db)
    assert result.returncode == 0, result.stderr
    assert "migrated as owner" in result.stdout
    assert OWNER_PASSWORD not in result.stdout + result.stderr
    assert APP_PASSWORD not in result.stdout + result.stderr


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ADMIN_DATABASE_URL": ""}, "ADMIN_DATABASE_URL is not set"),
        ({"MIGRATION_DATABASE_URL": ""}, "must both be set"),
        ({"CORE_LOAD": "sometimes"}, "CORE_LOAD must be"),
        ({"SEED_SCENARIOS": "true", "BUSINESS_DATE": "2026-06-01"}, "SEED_SCENARIOS needs"),
    ],
)
def test_script_refusals(
    first_run: BootstrapReport, fresh_db: FreshDb, env: dict[str, str], message: str
) -> None:
    result = run_script(fresh_db, **env)
    assert result.returncode == 1
    assert message in result.stderr
