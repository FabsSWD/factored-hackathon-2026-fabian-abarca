"""Create, update and verify the owner and application database roles (scripts/sql/roles.sql).

Run from the repository root, after `alembic upgrade head`, as the bootstrap superuser:

    python scripts/db_roles.py --admin-url postgresql://disputes:<password>@localhost:5432/postgres
    python scripts/db_roles.py --admin-url ... --verify     # only check (e.g. after a deploy)

Three roles:
- admin (--admin-url): the bootstrap superuser (POSTGRES_USER of the container). Never modified.
- owner (MIGRATION_DATABASE_URL): owns the database, tables and sequences.
- app (DATABASE_URL): the running application.

The target database is the one in DATABASE_URL, whatever database --admin-url names. The admin
password must be written in --admin-url (or ADMIN_DATABASE_URL); PGPASSWORD and password files
are ignored. Passwords are never printed. Any missing ownership or grant is an error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.settings import get_settings  # noqa: E402
from app.storage.roles import (  # noqa: E402
    RolesError,
    apply_roles,
    auth_method,
    connect_admin,
    role_targets,
    verify_roles,
)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--admin-url", default=os.environ.get("ADMIN_DATABASE_URL"))
    parser.add_argument("--verify", action="store_true", help="only verify, change nothing")
    args = parser.parse_args()
    settings = get_settings()
    if not args.admin_url:
        sys.exit("--admin-url (or ADMIN_DATABASE_URL) is required")
    if not settings.database_url or not settings.migration_database_url:
        sys.exit("DATABASE_URL and MIGRATION_DATABASE_URL must both be set")

    try:
        targets = role_targets(
            args.admin_url, settings.database_url, settings.migration_database_url
        )
        with connect_admin(targets) as conn:
            method = auth_method(conn)
            print(
                f"[roles] admin={targets.admin_user}@{targets.host} database={targets.database} "
                f"(password from --admin-url, auth method {method})"
            )
            if method == "trust":
                print("[roles] warning: the server accepted the admin without checking a password")
            if args.verify:
                verify_roles(conn, targets.owner_role, targets.app_role)
                print(f"[roles] verified: owner={targets.owner_role} app={targets.app_role}")
                return
            executed = apply_roles(conn, targets.values)
    except RolesError as exc:
        sys.exit(f"[roles] error: {exc}")
    except psycopg.OperationalError as exc:
        sys.exit(f"[roles] error: cannot connect as the admin: {exc}")
    print(
        f"[roles] applied and verified: owner={targets.owner_role} app={targets.app_role}, "
        f"{executed} statements"
    )


if __name__ == "__main__":
    main()
