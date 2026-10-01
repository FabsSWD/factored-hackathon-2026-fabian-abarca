"""Create or update the owner and application database roles (scripts/sql/roles.sql).

Run from the repository root, after `alembic upgrade head`, with a superuser connection:

    python scripts/db_roles.py --admin-url postgresql://postgres:<password>@localhost:5432/postgres

Role names and passwords come from MIGRATION_DATABASE_URL (owner) and DATABASE_URL (app) in
.env, and the target database is the one in DATABASE_URL. Passwords are never printed. The
admin URL can also be given as ADMIN_DATABASE_URL.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.settings import get_settings  # noqa: E402
from app.storage.roles import apply_roles  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--admin-url", default=os.environ.get("ADMIN_DATABASE_URL"))
    args = parser.parse_args()
    settings = get_settings()
    if not args.admin_url:
        sys.exit("--admin-url (or ADMIN_DATABASE_URL) is required")
    if not settings.database_url or not settings.migration_database_url:
        sys.exit("DATABASE_URL and MIGRATION_DATABASE_URL must both be set")

    app = make_url(settings.database_url)
    owner = make_url(settings.migration_database_url)
    if not (app.username and app.password and owner.username and owner.password):
        sys.exit("both URLs must include a user name and a password")
    if app.username == owner.username:
        sys.exit("the application and owner roles must be different")

    admin = make_url(args.admin_url).set(drivername="postgresql", database=app.database)
    with psycopg.connect(admin.render_as_string(hide_password=False), autocommit=True) as conn:
        executed = apply_roles(
            conn,
            {
                "owner_role": owner.username,
                "owner_password": owner.password,
                "app_role": app.username,
                "app_password": app.password,
            },
        )
    print(
        f"[roles] owner={owner.username} app={app.username} database={app.database}: "
        f"{executed} statements applied"
    )


if __name__ == "__main__":
    main()
