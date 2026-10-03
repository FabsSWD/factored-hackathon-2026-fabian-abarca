"""Seed the M17 evaluation scenarios as SEED- rows (or remove them).

    python scripts/seed_scenarios.py            # seed every case of config/eval_scenarios
    python scripts/seed_scenarios.py --remove   # delete every SEED- row, nothing else

Runs only with MIGRATION_DATABASE_URL (the owner role) and refuses the application's role. Also
needs DOCUMENT_HASH_KEY (each seeded customer logs in with its document number, SEED-0007, and
the TEST_OTP) and BUSINESS_DATE 2026-06-17: the scripts of the cases name dates of that day.
Idempotent: running it again puts every case back in the state of its specification (the
cases, blocks and handoffs left by earlier M18 runs are removed and their sessions revoked;
audit_logs is never touched). --remove refuses while audit records point to SEED- sessions.
The Core Banking loader never overwrites SEED- rows.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.evaluation.labeler import LABEL_BUSINESS_DATE  # noqa: E402
from app.evaluation.scenarios import load_scenarios, spec_version  # noqa: E402
from app.evaluation.seed import (  # noqa: E402
    SeedRemovalError,
    SeedRoleError,
    build_rows,
    count_seeded,
    remove,
    require_owner,
    require_owner_url,
    seed,
)
from app.settings import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--remove", action="store_true", help="delete every SEED- row")
    args = parser.parse_args()
    settings = get_settings()
    try:
        url = require_owner_url(settings.migration_database_url, settings.database_url)
    except SeedRoleError as exc:
        sys.exit(str(exc))
    engine = create_engine(url)
    with Session(engine) as session:
        try:
            require_owner(session)
        except SeedRoleError as exc:
            sys.exit(str(exc))
        if args.remove:
            try:
                removed = remove(session)
            except SeedRemovalError as exc:
                sys.exit(str(exc))
            session.commit()
            print(json.dumps({"removed": removed}, indent=2))
            return
        if settings.business_date != LABEL_BUSINESS_DATE:
            sys.exit(f"BUSINESS_DATE must be {LABEL_BUSINESS_DATE}: the case scripts name its dates")
        key = settings.document_hash_key.get_secret_value() if settings.document_hash_key else ""
        if not key:
            sys.exit("DOCUMENT_HASH_KEY is not set")
        version = spec_version()
        rows = build_rows(load_scenarios(), settings.business_date, key, version)
        report = seed(session, rows)
        session.commit()
        print(json.dumps({"version": version, "seeded": count_seeded(session),
                          "reset": report.removed}, indent=2))  # fmt: skip
    engine.dispose()


if __name__ == "__main__":
    main()
