"""Prepare the database before the API starts (M19): migrations, roles, Core Banking, seeds.

    python scripts/bootstrap_db.py

The `migrate` service of docker-compose.yml runs it on every start; it is idempotent (see
app/storage/bootstrap.py for the steps). Reads from the environment (or .env):

- ADMIN_DATABASE_URL: the bootstrap superuser (POSTGRES_USER), with its password in the URL.
- MIGRATION_DATABASE_URL and DATABASE_URL: the owner and app roles, created if missing.
- CORE_LOAD: auto (default: load data/core only while no real customer is loaded), always, never.
- SEED_SCENARIOS: true to seed the M17 scenarios (needs DOCUMENT_HASH_KEY and BUSINESS_DATE
  2026-06-17).

Exits with 1 and a message when the database cannot be prepared. Passwords are never printed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402

from app.settings import get_settings  # noqa: E402
from app.storage.bootstrap import (  # noqa: E402
    BootstrapConfig,
    BootstrapError,
    CoreLoad,
    bootstrap,
)
from app.storage.roles import RolesError  # noqa: E402

TRUE = {"1", "true", "yes", "on"}


def main() -> None:
    settings = get_settings()
    admin_url = os.environ.get("ADMIN_DATABASE_URL", "")
    if not admin_url:
        sys.exit("[bootstrap] error: ADMIN_DATABASE_URL is not set")
    if not settings.database_url or not settings.migration_database_url:
        sys.exit("[bootstrap] error: DATABASE_URL and MIGRATION_DATABASE_URL must both be set")
    try:
        core_load = CoreLoad(os.environ.get("CORE_LOAD", "auto").strip().lower() or "auto")
    except ValueError:
        sys.exit("[bootstrap] error: CORE_LOAD must be auto, always or never")
    key = settings.document_hash_key
    config = BootstrapConfig(
        admin_url=admin_url,
        database_url=settings.database_url,
        migration_url=settings.migration_database_url,
        core_dir=ROOT / "data" / "core",
        core_load=core_load,
        seed_scenarios=os.environ.get("SEED_SCENARIOS", "").strip().lower() in TRUE,
        document_hash_key=key.get_secret_value() if key else "",
        business_date=settings.business_date,
    )
    try:
        report = bootstrap(config)
    except (BootstrapError, RolesError) as exc:
        sys.exit(f"[bootstrap] error: {exc}")
    except psycopg.OperationalError as exc:
        sys.exit(f"[bootstrap] error: cannot connect: {exc}")
    print(
        f"[bootstrap] done: migrated as {report.migrated_as}, "
        f"{report.real_customers:,} customers loaded"
    )


if __name__ == "__main__":
    main()
