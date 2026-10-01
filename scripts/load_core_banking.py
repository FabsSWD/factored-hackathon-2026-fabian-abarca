"""Build core Parquet from silver and load it into PostgreSQL.

Pipeline (run from the repository root):

    python scripts/ingest.py customers products transactions   # raw CSV -> data/silver
    python scripts/load_core_banking.py                         # silver -> data/core -> PostgreSQL
    python scripts/load_core_banking.py --build-only            # stop after data/core

Reads BUSINESS_DATE, BUSINESS_DAY_CUTOFF, DOCUMENT_HASH_KEY and MIGRATION_DATABASE_URL (the
owner role; DATABASE_URL if unset) from .env, and
FX_MAX_STALENESS_DAYS from config/policy.yaml. Writes reports/core_build.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy.engine import make_url  # noqa: E402

from app.config import load_policy_config  # noqa: E402
from app.ingestion.core import CoreBuildConfig, build_core  # noqa: E402
from app.ingestion.loader import load_core_banking  # noqa: E402
from app.settings import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--build-only", action="store_true", help="do not load into PostgreSQL")
    parser.add_argument("--load-only", action="store_true", help="load existing data/core files")
    args = parser.parse_args()

    settings = get_settings()
    core_dir = ROOT / "data" / "core"

    if not args.load_only:
        key = settings.document_hash_key
        report = build_core(
            CoreBuildConfig(
                silver_dir=ROOT / "data" / "silver",
                exchange_rates_csv=ROOT / "data" / "raw" / "daily_exchange_rates.csv",
                core_dir=core_dir,
                business_date=settings.business_date,
                business_day_cutoff=settings.business_day_cutoff,
                fx_max_staleness_days=load_policy_config().parameters.FX_MAX_STALENESS_DAYS,
                document_hash_key=key.get_secret_value() if key else "",
            )
        )
        reports = ROOT / "reports"
        reports.mkdir(exist_ok=True)
        (reports / "core_build.json").write_text(
            json.dumps(asdict(report), indent=2, default=str), encoding="utf-8"
        )
        print(json.dumps(asdict(report), indent=2, default=str))

    if args.build_only:
        return
    if not settings.owner_database_url:
        sys.exit("MIGRATION_DATABASE_URL (or DATABASE_URL) is not set")

    import psycopg

    url = make_url(settings.owner_database_url).set(drivername="postgresql")
    with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
        for load in load_core_banking(connection, core_dir):
            print(
                f"[load] {load.table}: file={load.rows_in_file:,} "
                f"before={load.rows_before:,} after={load.rows_after:,}"
            )
        connection.commit()


if __name__ == "__main__":
    main()
