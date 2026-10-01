"""Database roles: scripts/sql/roles.sql, app/storage/roles.py and scripts/db_roles.py.

Reproduces the real deployment: the bootstrap superuser (the test server's user, like the
container's POSTGRES_USER) creates and migrates the database, so it owns everything. The
script then moves ownership to a separate owner role and grants the app role, verifying both.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import Iterator
from dataclasses import dataclass

import psycopg
import pytest
from alembic import command
from sqlalchemy.engine import URL

from app.storage.roles import (
    ROLES_SQL,
    RolesError,
    apply_roles,
    auth_method,
    connect_admin,
    problems,
    role_targets,
    statements,
    verify_roles,
)
from tests.conftest import ROOT, _admin_dsn, _test_server_url, alembic_config

CORE_BANKING = ("customers", "products", "transactions")
APP_WRITABLE = (
    "cases",
    "handoff_packets",
    "card_blocks",
    "audit_logs",
    "sessions",
    "otp_challenges",
    "otp_failures",
)
OWNER_PASSWORD = "owner-pass'with-quote"
APP_PASSWORD = "app-pass"


@dataclass(frozen=True)
class RolesDb:
    server: URL  # the bootstrap superuser on the maintenance database
    url: URL  # the bootstrap superuser on the target database
    owner: str
    app: str

    @property
    def admin(self) -> str:
        assert self.server.username is not None
        return self.server.username

    def role_url(self, role: str, password: str) -> str:
        return self.url.set(username=role, password=password).render_as_string(hide_password=False)

    @property
    def values(self) -> dict[str, str]:
        return {
            "owner_role": self.owner,
            "owner_password": OWNER_PASSWORD,
            "app_role": self.app,
            "app_password": APP_PASSWORD,
        }


@pytest.fixture(scope="module")
def roles_db() -> Iterator[RolesDb]:
    server = _test_server_url()
    suffix = uuid.uuid4().hex[:10]
    name, owner, app = f"disputes_roles_{suffix}", f"t_owner_{suffix}", f"t_app_{suffix}"
    # Created and migrated by the bootstrap user, as in the real deployment.
    with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    db = RolesDb(server, server.set(database=name), owner, app)
    try:
        command.upgrade(alembic_config(db.url), "head")
        yield db
    finally:
        with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            for role in (owner, app):
                admin.execute(f'DROP ROLE IF EXISTS "{role}"')


@pytest.fixture(scope="module")
def applied(roles_db: RolesDb) -> RolesDb:
    """The script run end to end against the bootstrap-owned database."""
    result = run_script(roles_db)
    assert result.returncode == 0, result.stderr
    assert "applied and verified" in result.stdout
    return roles_db


def run_script(
    db: RolesDb, *args: str, owner: str | None = None, app: str | None = None
) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "DATABASE_URL": db.role_url(app or db.app, APP_PASSWORD),
        "MIGRATION_DATABASE_URL": db.role_url(owner or db.owner, OWNER_PASSWORD),
    }
    env.pop("ADMIN_DATABASE_URL", None)
    admin_url = db.server.render_as_string(hide_password=False)
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "db_roles.py"), "--admin-url", admin_url, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


def query(url: URL, sql: str, *params: object) -> list[tuple[object, ...]]:
    with psycopg.connect(_admin_dsn(url)) as conn:
        return conn.execute(sql, params).fetchall()


def privilege(db: RolesDb, table: str, kind: str) -> bool:
    return bool(query(db.url, "SELECT has_table_privilege(%s, %s, %s)", db.app, table, kind)[0][0])


# --- The real case -------------------------------------------------------------------------


def test_bootstrap_owned_database_moves_to_the_owner(applied: RolesDb) -> None:
    owners = query(
        applied.url,
        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S')",
    )
    assert owners == [(applied.owner,)]
    database_owner = query(
        applied.url,
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
    )
    assert database_owner == [(applied.owner,)]
    alembic_owner = query(
        applied.url, "SELECT tableowner FROM pg_tables WHERE tablename = 'alembic_version'"
    )
    assert alembic_owner == [(applied.owner,)]


def test_bootstrap_user_is_never_modified(applied: RolesDb) -> None:
    row = query(applied.url, "SELECT rolsuper FROM pg_roles WHERE rolname = %s", applied.admin)
    assert row == [(True,)]


def test_verify_mode_passes_after_applying(applied: RolesDb) -> None:
    result = run_script(applied, "--verify")
    assert result.returncode == 0, result.stderr
    assert f"verified: owner={applied.owner} app={applied.app}" in result.stdout
    assert "password from --admin-url" in result.stdout


def test_output_never_contains_passwords(applied: RolesDb) -> None:
    result = run_script(applied)
    assert result.returncode == 0, result.stderr
    secret = applied.server.password or ""
    for password in (OWNER_PASSWORD, APP_PASSWORD, secret):
        assert password not in result.stdout + result.stderr


def test_rerun_is_idempotent(applied: RolesDb) -> None:
    assert run_script(applied).returncode == 0
    with connect(applied) as conn:
        assert problems(conn, applied.owner, applied.app) == []


@pytest.mark.parametrize("table", CORE_BANKING)
def test_app_reads_core_banking_only(applied: RolesDb, table: str) -> None:
    assert privilege(applied, table, "SELECT")
    for kind in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not privilege(applied, table, kind), kind


@pytest.mark.parametrize("table", APP_WRITABLE)
def test_app_writes_cases_and_audit(applied: RolesDb, table: str) -> None:
    for kind in ("SELECT", "INSERT", "UPDATE"):
        assert privilege(applied, table, kind), kind
    assert not privilege(applied, table, "TRUNCATE")
    deletable = table in ("otp_challenges", "otp_failures")
    assert privilege(applied, table, "DELETE") is deletable


def test_app_and_owner_log_in(applied: RolesDb) -> None:
    as_app = applied.url.set(username=applied.app, password=APP_PASSWORD)
    with psycopg.connect(_admin_dsn(as_app)) as c:
        assert c.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            c.execute("UPDATE products SET product_status = 'Blocked'")
    as_owner = applied.url.set(username=applied.owner, password=OWNER_PASSWORD)
    with psycopg.connect(_admin_dsn(as_owner)) as c:
        assert c.execute("SELECT current_user").fetchone() == (applied.owner,)


def test_owner_can_run_migrations(applied: RolesDb) -> None:
    owner_url = applied.url.set(username=applied.owner, password=OWNER_PASSWORD)
    command.downgrade(alembic_config(owner_url), "0003")
    command.upgrade(alembic_config(owner_url), "head")
    # New objects belong to the owner; the app grants are re-applied by re-running the script.
    assert run_script(applied).returncode == 0


# --- Refusals and verification -------------------------------------------------------------


def test_owner_equal_to_admin_is_refused_and_nothing_changes(roles_db: RolesDb) -> None:
    result = run_script(roles_db, owner=roles_db.admin)
    assert result.returncode != 0
    assert "the owner role" in result.stderr and "--admin-url" in result.stderr
    with connect(roles_db) as conn, pytest.raises(RolesError, match="is the admin user"):
        apply_roles(conn, {**roles_db.values, "owner_role": roles_db.admin})


def test_app_equal_to_admin_is_refused(roles_db: RolesDb) -> None:
    result = run_script(roles_db, app=roles_db.admin)
    assert result.returncode != 0
    assert "the app role" in result.stderr


def test_sql_guard_refuses_the_admin_as_owner(
    roles_db: RolesDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Bypass the Python check to exercise the guard that protects a plain psql run.
    from app.storage import roles

    monkeypatch.setattr(roles, "check_distinct_roles", lambda *args: None)
    guard = [s for s, _ in statements(ROLES_SQL.read_text()) if "RAISE EXCEPTION" in s]
    assert len(guard) == 1
    with connect(roles_db) as conn, pytest.raises(psycopg.errors.RaiseException):
        apply_roles(conn, {**roles_db.values, "owner_role": roles_db.admin}, guard[0] + " \\gexec")


def test_verify_reports_missing_and_extra_privileges(applied: RolesDb) -> None:
    with connect(applied) as conn:
        conn.execute(f'REVOKE SELECT ON customers FROM "{applied.app}"')
        conn.execute(f'GRANT DELETE ON cases TO "{applied.app}"')
        conn.execute(f'REVOKE USAGE ON SEQUENCE case_number_seq FROM "{applied.app}"')
        try:
            with pytest.raises(RolesError) as caught:
                verify_roles(conn, applied.owner, applied.app)
            message = str(caught.value)
            assert f"{applied.app} lacks SELECT on customers" in message
            assert f"{applied.app} must not have DELETE on cases" in message
            assert "lacks USAGE on sequence case_number_seq" in message
            assert run_script(applied, "--verify").returncode != 0
        finally:
            apply_roles(conn, applied.values)


def test_verify_reports_unowned_and_unknown_tables(applied: RolesDb) -> None:
    with connect(applied) as conn:
        conn.execute("CREATE TABLE stray (id int)")
        try:
            found = problems(conn, applied.owner, applied.app)
            assert f"table stray is owned by {applied.admin}, not {applied.owner}" in found
            assert "table stray has no privilege rule (update app/storage/roles.py)" in found
        finally:
            conn.execute("DROP TABLE stray")


def test_verify_reports_missing_roles_and_database_owner(roles_db: RolesDb) -> None:
    with connect(roles_db) as conn:
        assert problems(conn, "t_nobody_x", "t_nobody_y") == [
            "role t_nobody_x does not exist",
            "role t_nobody_y does not exist",
        ]
        conn.execute("CREATE ROLE t_super_tmp LOGIN SUPERUSER")
        conn.execute("CREATE ROLE t_nologin_tmp NOLOGIN")
        try:
            found = problems(conn, "t_super_tmp", "t_nologin_tmp")
            assert "role t_super_tmp must be NOSUPERUSER NOCREATEROLE NOCREATEDB" in found
            assert "role t_nologin_tmp cannot log in" in found
        finally:
            conn.execute("DROP ROLE t_super_tmp")
            conn.execute("DROP ROLE t_nologin_tmp")


def test_verify_reports_tables_and_sequences_that_are_missing(applied: RolesDb) -> None:
    with connect(applied) as conn:
        conn.execute("ALTER TABLE card_blocks RENAME TO card_blocks_old")
        conn.execute("ALTER SEQUENCE case_number_seq RENAME TO case_number_old")
        try:
            found = problems(conn, applied.owner, applied.app)
            assert "table card_blocks is missing (run alembic upgrade head first)" in found
            assert "sequence case_number_seq is missing (run alembic upgrade head first)" in found
        finally:
            conn.execute("ALTER TABLE card_blocks_old RENAME TO card_blocks")
            conn.execute("ALTER SEQUENCE case_number_old RENAME TO case_number_seq")


def test_verify_reports_database_owner_schema_and_connect(applied: RolesDb) -> None:
    with connect(applied) as conn:
        conn.execute(f'ALTER DATABASE "{applied.url.database}" OWNER TO "{applied.admin}"')
        conn.execute(f'GRANT CREATE ON SCHEMA public TO "{applied.app}"')
        conn.execute(f'REVOKE CONNECT ON DATABASE "{applied.url.database}" FROM "{applied.app}"')
        try:
            found = problems(conn, applied.owner, applied.app)
            assert f"database is owned by {applied.admin}, not {applied.owner}" in found
            assert f"{applied.app} must not have CREATE on schema public" in found
            assert f"{applied.app} lacks CONNECT on the database" in found
        finally:
            conn.execute(f'REVOKE CREATE ON SCHEMA public FROM "{applied.app}"')
            apply_roles(conn, applied.values)


# --- URLs and the admin connection ---------------------------------------------------------


ADMIN = "postgresql://disputes:secret@localhost:5432/postgres"
APP = "postgresql+psycopg://disputes_app:a@localhost:5432/disputes"
OWNER = "postgresql+psycopg://disputes_owner:o@localhost:5432/disputes"


def test_role_targets() -> None:
    targets = role_targets(ADMIN, APP, OWNER)
    assert (targets.admin_user, targets.owner_role, targets.app_role) == (
        "disputes",
        "disputes_owner",
        "disputes_app",
    )
    assert targets.database == "disputes"
    assert targets.admin_dsn.endswith("/disputes")  # the target database, not the admin's
    assert "secret" not in repr(targets) and "o@" not in repr(targets)
    assert targets.values == {
        "owner_role": "disputes_owner",
        "owner_password": "o",
        "app_role": "disputes_app",
        "app_password": "a",
    }


@pytest.mark.parametrize(
    ("admin", "app", "owner", "message"),
    [
        ("postgresql://disputes:@localhost/postgres", APP, OWNER, "must include the admin user"),
        ("postgresql://disputes@localhost/postgres", APP, OWNER, "its password"),
        (ADMIN, "postgresql://disputes_app@localhost/disputes", OWNER, "user and password"),
        (ADMIN, "postgresql://disputes_app:a@localhost", OWNER, "name the target database"),
        (ADMIN, APP, "postgresql://disputes_owner:o@localhost/other", "same database"),
        (ADMIN, APP, "postgresql://disputes:o@localhost/disputes", "the owner role (disputes)"),
        (ADMIN, "postgresql://disputes:a@localhost/disputes", OWNER, "the app role (disputes)"),
        (ADMIN, APP, "postgresql://disputes_app:o@localhost/disputes", "must be different"),
    ],
)
def test_role_targets_refusals(admin: str, app: str, owner: str, message: str) -> None:
    with pytest.raises(RolesError, match=message.replace("(", r"\(").replace(")", r"\)")):
        role_targets(admin, app, owner)


def connect(db: RolesDb) -> psycopg.Connection:
    """The admin connection to the target database, as scripts/db_roles.py opens it."""
    targets = role_targets(
        db.server.render_as_string(hide_password=False),
        db.role_url(db.app, APP_PASSWORD),
        db.role_url(db.owner, OWNER_PASSWORD),
    )
    return connect_admin(targets)


def test_admin_connection_ignores_pgpassword(
    roles_db: RolesDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PGPASSWORD", "not-the-password")
    with connect(roles_db) as conn:
        assert conn.execute("SELECT current_database()").fetchone() == (roles_db.url.database,)
        assert auth_method(conn) != ""
    assert os.environ["PGPASSWORD"] == "not-the-password"


def test_trust_is_reported_when_no_password_was_checked() -> None:
    class Trusting:
        def execute(self, *args: object) -> Trusting:
            return self

        def fetchone(self) -> tuple[None]:
            return (None,)

    assert auth_method(Trusting()) == "trust"  # type: ignore[arg-type]


def test_statement_parser() -> None:
    parsed = statements(ROLES_SQL.read_text())
    assert parsed and all(text and "\\gexec" not in text for text, _ in parsed)
    assert statements("-- c\n\\set X on\nSELECT 1\n\\gexec\nSELECT\n2;\n") == [
        ("SELECT 1", True),
        ("SELECT 2", False),
    ]
    with pytest.raises(ValueError, match="unterminated"):
        statements("SELECT 1")


def test_missing_variables_are_rejected() -> None:
    with pytest.raises(ValueError, match="missing variables"):
        apply_roles(None, {"owner_role": "x"})  # type: ignore[arg-type]
