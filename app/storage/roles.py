"""Apply ``scripts/sql/roles.sql`` without psql.

The SQL file is the single definition of the database roles and is written for psql
(``:'name'`` variables and ``\\gexec``). This runner understands exactly that subset, so the
same file can be applied from Python (``scripts/db_roles.py``) and tested.
"""

from __future__ import annotations

import re
from pathlib import Path

import psycopg
from psycopg import sql

ROLES_SQL = Path(__file__).resolve().parents[2] / "scripts" / "sql" / "roles.sql"
VARIABLES = ("owner_role", "owner_password", "app_role", "app_password")
_VARIABLE = re.compile(r":'(\w+)'")


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


def apply_roles(conn: psycopg.Connection, values: dict[str, str], text: str | None = None) -> int:
    """Apply the roles file on ``conn`` (a superuser connection to the application database).
    Returns the number of statements executed. Values are bound as parameters, never pasted."""
    missing = set(VARIABLES) - set(values)
    if missing:
        raise ValueError(f"missing variables: {', '.join(sorted(missing))}")
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
    return executed
