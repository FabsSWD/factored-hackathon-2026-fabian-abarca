"""Run the M18 evaluation: the system and the baseline on the evaluation split.

    python scripts/run_evaluation.py --dry-run          # guards, cases, estimate; no model call
    python scripts/run_evaluation.py                    # 3 system runs, 1 with connect off, baseline
    python scripts/run_evaluation.py --cases S001,R005 --runs 1 --skip-baseline   # smoke run
    python scripts/run_evaluation.py --retry-baseline-failures   # only the failed baseline calls

Needs, from .env: DATABASE_URL, MIGRATION_DATABASE_URL (the owner role: seeding again before
each run and removing the cases a run creates on real customers), DOCUMENT_HASH_KEY, TEST_OTP,
PSEUDONYM_KEY, JWT_SECRET, OPENAI_API_KEY and LLM_MODEL; KEV_BASE_URL if Kev is available;
LLM_*_USD_PER_MTOK for costs; BUSINESS_DATE 2026-06-17. Real cases need
config/eval_scenarios/local/ (scripts/select_real_scenarios.py) and data/raw/customers.csv.

Writes reports/m18/results.csv (git-ignored), reports/m18_evaluation.json (the M16 dashboard)
and docs/evaluation.md. A smoke run (--cases) writes reports/m18_evaluation_partial.json and
leaves docs/evaluation.md alone. Exits with 2 when any system run misses a hard rule, and with
3 when a baseline call failed (zero errors is required to publish the comparison).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.audit.cost import rates_from_settings  # noqa: E402
from app.config import load_policy_config  # noqa: E402
from app.evaluation.harness import guards  # noqa: E402
from app.evaluation.harness.baseline import system_prompt  # noqa: E402
from app.evaluation.harness.estimate import estimate  # noqa: E402
from app.evaluation.harness.metrics import (  # noqa: E402
    HardRuleFalseNegativeError,
    assert_no_hard_rule_false_negatives,
)
from app.evaluation.harness.report import BASELINE  # noqa: E402
from app.evaluation.harness.runner import (  # noqa: E402
    Options,
    load_cases,
    retry_baseline_failures,
    run_evaluation,
    write_outputs,
)
from app.settings import get_settings  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--dry-run", action="store_true", help="guards, cases and estimate only")
    parser.add_argument("--runs", type=int, default=3, help="system runs with connect on")
    parser.add_argument("--skip-no-connect", action="store_true", help="no run with connect off")
    parser.add_argument("--skip-baseline", action="store_true", help="no baseline")
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--cases", default="", help="comma-separated case keys (smoke run)")
    parser.add_argument(
        "--retry-baseline-failures",
        action="store_true",
        help="call the baseline again where it failed and merge into the last run's outputs",
    )
    args = parser.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    settings = get_settings()
    policy = load_policy_config()
    options = Options(
        runs=args.runs,
        connect_off_run=not args.skip_no_connect,
        baseline=not args.skip_baseline,
        concurrency=args.concurrency,
        only=frozenset(key.strip() for key in args.cases.split(",") if key.strip()),
    )
    try:
        split = guards.check_all(policy)
        if args.dry_run:
            cases = load_cases(settings, split.evaluation, options)
            print(f"guards hold; {len(cases)} evaluation cases "
                  f"({sum(c.data_source == 'real' for c in cases)} real) load and resolve")  # fmt: skip
            figures = estimate(len(cases), options.runs, options.connect_off_run, options.baseline,
                               options.concurrency, len(system_prompt()),
                               rates_from_settings(settings))  # fmt: skip
            print("\n".join(figures.lines()))
            return
        if args.retry_baseline_failures:
            for path in retry_baseline_failures(settings, policy, options):
                print(f"written {path.relative_to(ROOT)}")
            merged = json.loads((ROOT / "reports" / "m18_evaluation.json").read_text("utf-8"))
            left = merged["arms"][BASELINE]["pooled"]["errors"]
            if left:
                print(f"not publishable: {left} baseline calls still failed", file=sys.stderr)
                sys.exit(3)
            return
        evaluation = run_evaluation(settings, policy, options)
    except guards.HarnessGuardError as exc:
        sys.exit(f"refused: {exc}")
    for path in write_outputs(evaluation, options):
        print(f"written {path.relative_to(ROOT)}")
    try:
        for name, arm in evaluation.arms.items():
            if name != BASELINE:
                assert_no_hard_rule_false_negatives(arm.scores)
    except HardRuleFalseNegativeError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(2)
    baseline = evaluation.arms.get(BASELINE)
    errors = sum(p.error is not None for p in baseline.predictions) if baseline else 0
    if errors:
        print(f"not publishable: {errors} baseline calls failed", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
