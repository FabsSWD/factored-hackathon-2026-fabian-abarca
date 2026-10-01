"""Apply and verify ``scripts/sql/roles.sql`` without psql.

Three roles take part:

- the admin (bootstrap superuser, e.g. the container's ``POSTGRES_USER``): runs this module and
  is never modified by it;
- the owner (``MIGRATION_DATABASE_URL``): owns the database and every table and sequence;
- the app (``DATABASE_URL``): the running application, with the grants below.

The SQL file is the single definition of the roles and is written for psql (``:'name'``
variables and ``\\gexec``); this runner understands exactly that subset. The verification does
not trust the SQL file: it checks ownership and privileges in the catalog independently, so
a missing grant is an error, never a silent success.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROLES_SQL = Path(__file__).resolve().parents[2] / "scripts" / "sql" / "roles.sql"
VARIABLES = ("owner_role", "owner_password", "app_role", "app_password")
_VARIABLE = re.compile(r":'(\w+)'")

# The privilege matrix the verification enforces (keep in sync with scripts/sql/roles.sql).
CORE_BANKING = frozenset({"customers", "products", "transactions"})
APP_WRITABLE = frozenset(
    {
        "cases",
        "handoff_packets",
        "card_blocks",
        "audit_logs",
        "sessions",
        "otp_challenges",
        "otp_failures",
    }
)
APP_DELETABLE = frozenset({"otp_challenges", "otp_failures"})
APP_NO_ACCESS = frozenset({"alembic_version"})
APP_SEQUENCES = frozenset({"case_number_seq", "audit_logs_id_seq", "otp_failures_id_seq"})
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")


class RolesError(RuntimeError):
    """The roles could not be applied, or the database does not match the expected state."""


def statements(text: str) -> list[tuple[str, bool]]:
    """``(statement, gexec)`` pairs: comments and ``\\set`` lines dropped; a statement ends
    with ``;`` or with ``\\gexec`` (its result rows are statements to run)."""
    found: list[tuple[str, bool]] = []
    current: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--") or stripped.startswith("\\set"):
            continue
        if stripped.endswith("\\gexec"):
            current.append(stripped.removesuffix("\\gexec"))
            found.append((" ".join(current).strip(), True))
            current = []
        elif stripped.endswith(";"):
            current.append(stripped.removesuffix(";"))
            found.append((" ".join(current).strip(), False))
            current = []
        else:
            current.append(stripped)
    if current:
        raise ValueError(f"unterminated statement: {' '.join(current)}")
    return found


def check_distinct_roles(admin_role: str, owner_role: str, app_role: str) -> None:
    """The admin is never modified, and the owner and the app are different roles."""
    if owner_role == admin_role:
        raise RolesError(
            f"the owner role ({owner_role}) is the admin user of --admin-url: use a separate "
            "owner role in MIGRATION_DATABASE_URL (the bootstrap superuser cannot lose SUPERUSER)"
        )
    if app_role == admin_role:
        raise RolesError(f"the app role ({app_role}) is the admin user of --admin-url")
    if owner_role == app_role:
        raise RolesError("the owner and app roles must be different")


def apply_roles(conn: psycopg.Connection, values: dict[str, str], text: str | None = None) -> int:
    """Apply the roles file on ``conn`` (an admin connection to the target database), then
    verify the result. Returns the number of statements executed. Values are bound as
    parameters, never pasted into SQL."""
    missing = set(VARIABLES) - set(values)
    if missing:
        raise ValueError(f"missing variables: {', '.join(sorted(missing))}")
    admin_role = str(_scalar(conn, "SELECT current_user"))
    check_distinct_roles(admin_role, values["owner_role"], values["app_role"])
    executed = 0
    for statement, gexec in statements(text if text is not None else ROLES_SQL.read_text()):
        names = _VARIABLE.findall(statement)
        # format() is variadic: the server cannot infer a parameter type, so bind them as text.
        query = sql.SQL(_VARIABLE.sub("%s::text", statement.replace("%", "%%")))
        params = [values[name] for name in names]
        if not gexec:
            conn.execute(query, params)
            executed += 1
            continue
        for (generated,) in conn.execute(query, params).fetchall():
            conn.execute(sql.SQL(generated))  # built by format() with %I and %L
            executed += 1
    verify_roles(conn, values["owner_role"], values["app_role"])
    return executed


def problems(conn: psycopg.Connection, owner_role: str, app_role: str) -> list[str]:
    """Every difference between the database and the expected ownership and privileges."""
    found: list[str] = []
    for role in (owner_role, app_role):
        row = conn.execute(
            "SELECT rolsuper, rolcreaterole, rolcreatedb, rolcanlogin FROM pg_roles "
            "WHERE rolname = %s",
            (role,),
        ).fetchone()
        if row is None:
            found.append(f"role {role} does not exist")
            continue
        if row[:3] != (False, False, False):
            found.append(f"role {role} must be NOSUPERUSER NOCREATEROLE NOCREATEDB")
        if not row[3]:
            found.append(f"role {role} cannot log in")
    if found:
        return found  # the privilege functions fail on a missing role

    database_owner = _scalar(
        conn,
        "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = current_database()",
    )
    if database_owner != owner_role:
        found.append(f"database is owned by {database_owner}, not {owner_role}")
    objects = conn.execute(
        "SELECT c.relname, c.relkind, pg_get_userbyid(c.relowner) FROM pg_class c "
        "JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'S') ORDER BY c.relname"
    ).fetchall()
    tables = {name for name, kind, _ in objects if kind == "r"}
    sequences = {name for name, kind, _ in objects if kind == "S"}
    for name, kind, owner in objects:
        if owner != owner_role:
            label = "table" if kind == "r" else "sequence"
            found.append(f"{label} {name} is owned by {owner}, not {owner_role}")

    known = CORE_BANKING | APP_WRITABLE | APP_NO_ACCESS
    for table in sorted(tables - known):
        found.append(f"table {table} has no privilege rule (update app/storage/roles.py)")
    for table in sorted((known - APP_NO_ACCESS) - tables):
        found.append(f"table {table} is missing (run alembic upgrade head first)")
    for table in sorted(tables & known):
        if table in CORE_BANKING:
            expected = {"SELECT"}
        elif table in APP_WRITABLE:
            expected = {"SELECT", "INSERT", "UPDATE"} | (
                {"DELETE"} if table in APP_DELETABLE else set()
            )
        else:
            expected = set()
        for privilege in TABLE_PRIVILEGES:
            granted = _scalar(
                conn,
                "SELECT has_table_privilege(%s, %s, %s)",
                app_role,
                f"public.{table}",
                privilege,
            )
            if granted != (privilege in expected):
                state = "lacks" if privilege in expected else "must not have"
                found.append(f"{app_role} {state} {privilege} on {table}")
    for sequence in sorted(APP_SEQUENCES):
        if sequence not in sequences:
            found.append(f"sequence {sequence} is missing (run alembic upgrade head first)")
        elif not _scalar(
            conn,
            "SELECT has_sequence_privilege(%s, %s, 'USAGE')",
            app_role,
            f"public.{sequence}",
        ):
            found.append(f"{app_role} lacks USAGE on sequence {sequence}")
    if _scalar(conn, "SELECT has_schema_privilege(%s, 'public', 'CREATE')", app_role):
        found.append(f"{app_role} must not have CREATE on schema public")
    if not _scalar(
        conn, "SELECT has_database_privilege(%s, current_database(), 'CONNECT')", app_role
    ):
        found.append(f"{app_role} lacks CONNECT on the database")
    return found


def verify_roles(conn: psycopg.Connection, owner_role: str, app_role: str) -> None:
    """Raise ``RolesError`` listing every problem; return quietly only when all checks pass."""
    found = problems(conn, owner_role, app_role)
    if found:
        raise RolesError("roles verification failed:\n- " + "\n- ".join(found))


def _scalar(conn: psycopg.Connection, query: str, *params: object) -> object:
    row = conn.execute(query, params).fetchone()
    assert row is not None
    return row[0]


# --- Connection settings for scripts/db_roles.py ---------------------------------------------


@dataclass(frozen=True)
class RoleTargets:
    """The admin connection and the two roles, validated from the three URLs."""

    admin_dsn: str = field(repr=False)
    admin_user: str
    host: str
    database: str
    owner_role: str
    owner_password: str = field(repr=False)
    app_role: str
    app_password: str = field(repr=False)

    @property
    def values(self) -> dict[str, str]:
        return {
            "owner_role": self.owner_role,
            "owner_password": self.owner_password,
            "app_role": self.app_role,
            "app_password": self.app_password,
        }


def role_targets(admin_url: str, database_url: str, migration_url: str) -> RoleTargets:
    """Validate the URLs. The admin password must be written in ``admin_url``: it is never
    taken from PGPASSWORD, a password file or another setting."""
    admin, app, owner = make_url(admin_url), make_url(database_url), make_url(migration_url)
    if not admin.username or not admin.password:
        raise RolesError(
            "--admin-url must include the admin user and its password "
            "(postgresql://<user>:<password>@host:port/db); no other source is used"
        )
    if not (app.username and app.password and owner.username and owner.password):
        raise RolesError("DATABASE_URL and MIGRATION_DATABASE_URL must include user and password")
    if not app.database:
        raise RolesError("DATABASE_URL must name the target database")
    if owner.database and owner.database != app.database:
        raise RolesError("DATABASE_URL and MIGRATION_DATABASE_URL must use the same database")
    check_distinct_roles(admin.username, owner.username, app.username)
    target = admin.set(drivername="postgresql", database=app.database)
    return RoleTargets(
        admin_dsn=target.render_as_string(hide_password=False),
        admin_user=admin.username,
        host=f"{admin.host or 'localhost'}:{admin.port or 5432}",
        database=app.database,
        owner_role=owner.username,
        owner_password=owner.password,
        app_role=app.username,
        app_password=app.password,
    )


def connect_admin(targets: RoleTargets) -> psycopg.Connection:
    """Connect to the target database as the admin with only the password of --admin-url:
    PGPASSWORD and password files are ignored, so a missing password cannot be filled in
    silently."""
    saved = os.environ.pop("PGPASSWORD", None)
    try:
        return psycopg.connect(targets.admin_dsn, autocommit=True, passfile=os.devnull)
    finally:
        if saved is not None:
            os.environ["PGPASSWORD"] = saved


def auth_method(conn: psycopg.Connection) -> str:
    """How the server authenticated the admin (PostgreSQL 16 ``system_user``); ``trust`` means
    the server did not check the password at all."""
    value = _scalar(conn, "SELECT system_user")
    return str(value).split(":", 1)[0] if value is not None else "trust"
