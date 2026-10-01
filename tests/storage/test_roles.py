"""scripts/sql/roles.sql, applied to a fresh migrated database with throwaway roles."""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import psycopg
import pytest
from alembic import command
from sqlalchemy.engine import URL

from app.storage.roles import ROLES_SQL, apply_roles, statements
from tests.conftest import _admin_dsn, _test_server_url, alembic_config

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


@pytest.fixture(scope="module")
def roles_db() -> Iterator[tuple[URL, str, str]]:
    server = _test_server_url()
    suffix = uuid.uuid4().hex[:10]
    name, owner, app = f"disputes_roles_{suffix}", f"t_owner_{suffix}", f"t_app_{suffix}"
    with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
        admin.execute(f'CREATE DATABASE "{name}"')
    url = server.set(database=name)
    try:
        command.upgrade(alembic_config(url), "head")
        with psycopg.connect(_admin_dsn(url), autocommit=True) as conn:
            values = {
                "owner_role": owner,
                "owner_password": "owner-pass'with-quote",
                "app_role": app,
                "app_password": "app-pass",
            }
            apply_roles(conn, values)
            apply_roles(conn, values)  # idempotent
        yield url, owner, app
    finally:
        with psycopg.connect(_admin_dsn(server), autocommit=True) as admin:
            admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
            for role in (owner, app):
                admin.execute(f'DROP ROLE IF EXISTS "{role}"')


def query(url: URL, sql: str, *params: object) -> list[tuple[object, ...]]:
    with psycopg.connect(_admin_dsn(url)) as conn:
        return conn.execute(sql, params).fetchall()


def privilege(url: URL, role: str, table: str, kind: str) -> bool:
    return bool(query(url, "SELECT has_table_privilege(%s, %s, %s)", role, table, kind)[0][0])


@pytest.mark.parametrize("table", CORE_BANKING)
def test_app_reads_core_banking_only(roles_db: tuple[URL, str, str], table: str) -> None:
    url, _, app = roles_db
    assert privilege(url, app, table, "SELECT")
    for kind in ("INSERT", "UPDATE", "DELETE", "TRUNCATE"):
        assert not privilege(url, app, table, kind), kind


@pytest.mark.parametrize("table", APP_WRITABLE)
def test_app_writes_cases_and_audit(roles_db: tuple[URL, str, str], table: str) -> None:
    url, _, app = roles_db
    for kind in ("SELECT", "INSERT", "UPDATE"):
        assert privilege(url, app, table, kind), kind
    assert not privilege(url, app, table, "TRUNCATE")
    deletable = table in ("otp_challenges", "otp_failures")
    assert privilege(url, app, table, "DELETE") is deletable


def test_app_cannot_touch_migrations_or_ddl(roles_db: tuple[URL, str, str]) -> None:
    url, _, app = roles_db
    assert not privilege(url, app, "alembic_version", "SELECT")
    assert not query(url, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", app)[0][0]


def test_app_uses_the_sequences(roles_db: tuple[URL, str, str]) -> None:
    url, _, app = roles_db
    for sequence in ("case_number_seq", "audit_logs_id_seq", "otp_failures_id_seq"):
        assert query(url, "SELECT has_sequence_privilege(%s, %s, 'USAGE')", app, sequence)[0][0]


def test_owner_owns_every_table_and_sequence(roles_db: tuple[URL, str, str]) -> None:
    url, owner, _ = roles_db
    owners = query(
        url,
        "SELECT DISTINCT pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S')",
    )
    assert owners == [(owner,)]


def test_roles_are_not_privileged_and_can_log_in(roles_db: tuple[URL, str, str]) -> None:
    url, owner, app = roles_db
    rows = query(
        url,
        "SELECT rolname, rolcanlogin, rolsuper, rolcreatedb, rolcreaterole FROM pg_roles "
        "WHERE rolname IN (%s, %s) ORDER BY rolname",
        owner,
        app,
    )
    assert [row[1:] for row in rows] == [(True, False, False, False)] * 2


def test_app_role_can_log_in_and_is_refused_writes(roles_db: tuple[URL, str, str]) -> None:
    url, _, app = roles_db
    as_app = url.set(username=app, password="app-pass")
    with psycopg.connect(_admin_dsn(as_app)) as conn:
        assert conn.execute("SELECT count(*) FROM transactions").fetchone() == (0,)
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute("UPDATE products SET product_status = 'Blocked'")


def test_owner_password_with_a_quote_is_bound_safely(roles_db: tuple[URL, str, str]) -> None:
    url, owner, _ = roles_db
    as_owner = url.set(username=owner, password="owner-pass'with-quote")
    with psycopg.connect(_admin_dsn(as_owner)) as conn:
        assert conn.execute("SELECT current_user").fetchone() == (owner,)


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
