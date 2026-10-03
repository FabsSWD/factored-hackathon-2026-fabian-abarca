"""Label the M17 scenarios, fix the split and draw the human review sample.

    python scripts/label_scenarios.py           # write labels, split and mix
    python scripts/label_scenarios.py --check   # fail if any of them differs from a fresh run
    python scripts/label_scenarios.py --review  # also write the review sample (reads the database)

Writes:
- config/eval_scenarios/labels.yaml: the reference label of each seeded case (the policy engine
  on the specification, policy §16.2). The real cases come from real_selection.lock: their case
  keys, categories and expected labels, without identifiers (scripts/select_real_scenarios.py);
- config/eval_scenarios/split.yaml: calibration and evaluation cases of both sources, by case
  key (policy §16.4), so it is reproducible from the repository. It is written once; --check
  fails if a fresh split differs, and a test pins its fingerprint;
- reports/m17_mix.json: the mix by source, outcome, rule, language, path and split;
- with --review, reports/m17_review/review_sample.csv: at least 10% of the cases, minimum 50 (policy
  §16.3), with every ESC-03, ESC-06 and ESC-13 case and at least 15 real cases. Real rows show
  the records read from the database (DATABASE_URL, read-only) for the cases of
  config/eval_scenarios/local/, so the file is derived data: it is git-ignored and never
  committed. It holds no customer or transaction identifier and no document number.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import random
import sys
from collections import Counter
from collections.abc import Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import yaml  # noqa: E402

from app.evaluation.labeler import LABEL_BUSINESS_DATE, CaseLabel, label_all  # noqa: E402
from app.evaluation.real import (  # noqa: E402
    LockedCase,
    label_as_of,
    load_lock,
)
from app.evaluation.scenarios import (  # noqa: E402
    DEFAULT_SCENARIOS_DIR,
    Scenario,
    load_scenarios,
    spec_version,
)
from app.evaluation.split import Split, make_split  # noqa: E402

REVIEW_SEED = 20261003
REVIEW_FILE = ROOT / "reports" / "m17_review" / "review_sample.csv"  # git-ignored (*.csv)
ALWAYS_REVIEWED = ("ESC-03", "ESC-06", "ESC-13")
MIN_REAL_REVIEWED = 15
POLICY_REFERENCE = {
    "GATE-01": "§5 GATE-01", "GATE-02": "§5 GATE-02", "GATE-03": "§5 GATE-03", "GATE-04": "§5 GATE-04",
    "GATE-05": "§5 GATE-05", "GATE-06": "§5 GATE-06", "GATE-07": "§4, §5 GATE-07", "GATE-08": "§5 GATE-08",
    "GATE-09": "§5 GATE-09", "GATE-10": "§5 GATE-10", "GATE-11": "§5 GATE-11",
}  # fmt: skip
AnyCase = Scenario | LockedCase


def labels_yaml(labels: list[CaseLabel], version: str) -> str:
    body = {"spec_version": version, "labels": [label.model_dump(mode="json") for label in labels]}
    return (
        "# Reference labels: the policy engine on each specification (scripts/label_scenarios.py).\n"
        + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100)
    )


def split_yaml(split: Split) -> str:
    body = {"seed": split.seed, "fingerprint": split.fingerprint,
            "calibration": split.calibration, "evaluation": split.evaluation}  # fmt: skip
    return ("# Fixed before any tuning (policy §16.4). Calibration: M7 only. Evaluation: M18, never\n"
            "# used to set a Calibrated parameter. Do not edit; a test pins the fingerprint.\n"
            + yaml.safe_dump(body, sort_keys=False, width=100))  # fmt: skip


def review_sample(cases: Sequence[AnyCase], labels: list[CaseLabel], size: int) -> list[str]:
    """Every ESC-03, ESC-06 and ESC-13 case; at least MIN_REAL_REVIEWED real cases, one of
    each category first; the rest of the seeded cases stratified by path and language."""
    by_id = {label.case_id: label for label in labels}
    chosen = [c.id for c in cases if set(by_id[c.id].triggered_rules) & set(ALWAYS_REVIEWED)]
    rng = random.Random(REVIEW_SEED)
    real = sorted((c for c in cases if isinstance(c, LockedCase)), key=lambda c: c.id)
    categories: dict[str, list[str]] = {}
    for c in real:
        categories.setdefault(c.category, []).append(c.id)
    for category in sorted(categories):
        chosen.append(rng.choice(categories[category]))
    others = [c.id for c in real if c.id not in chosen]
    rng.shuffle(others)
    chosen += others[: max(0, MIN_REAL_REVIEWED - sum(1 for c in real if c.id in chosen))]
    rest = [c for c in cases if isinstance(c, Scenario) and c.id not in chosen]
    strata: dict[tuple[str, str], list[str]] = {}
    for s in rest:
        strata.setdefault((s.path.value, s.language), []).append(s.id)
    remaining = max(0, size - len(chosen))
    quotas = {key: round(remaining * len(ids) / len(rest)) for key, ids in strata.items()}
    for key in sorted(strata):
        ids = sorted(strata[key])
        rng.shuffle(ids)
        chosen += ids[: quotas[key]]
    leftovers = [s.id for s in rest if s.id not in chosen]
    rng.shuffle(leftovers)
    chosen += leftovers[: max(0, size - len(chosen))]
    return sorted(chosen)


def _records(s: Scenario) -> str:
    txns = "; ".join(
        f"{t.key}: {t.type} {t.amount} {t.currency} {t.merchant or ''} {t.days_ago}d ago {t.status}"
        f"{' fraud ' + str(t.fraud_score) if t.fraud_score and t.fraud_score > 35 else ''}"
        f"{' (other customer)' if t.foreign else ''}"
        for t in s.transactions
    )
    products = "; ".join(f"{p.key}: {p.type} {p.status}" for p in s.products)
    prior = "; ".join(f"case on {c.transaction} {c.days_ago}d ago" for c in s.prior_cases)
    return f"customer {s.customer.status}; {products}; {txns}" + (f"; {prior}" if prior else "")


def _intent(c: Scenario) -> str:
    parts = []
    for d in c.disputes:
        values = d.model_dump(exclude_none=True, exclude_defaults=True)
        parts.append(", ".join(f"{k}={v}" for k, v in values.items()))
    return " | ".join(parts)


def _conditions(c: AnyCase) -> str:
    defaults = type(c.conditions)()
    return ", ".join(
        f"{name}={value}" for name, value in c.conditions if value != getattr(defaults, name)
    )


def review_csv(
    cases: Sequence[AnyCase],
    labels: list[CaseLabel],
    split: Split,
    chosen: list[str],
    real_rows: dict[str, tuple[str, str, str]],
) -> str:
    """``real_rows``: per real case, its records, intent and first message, read at run time
    from the local cases (``real_review_rows``)."""
    by_id = {label.case_id: label for label in labels}
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["case_id", "title", "data_source", "language", "path", "split", "records",
                     "intent_and_final_slots", "special_conditions", "first_message",
                     "expected_outcome", "expected_rules", "failed_gates", "queue", "priority",
                     "inform_reason", "expected_actions", "policy_reference", "reviewer_verdict",
                     "reviewer_notes"])  # fmt: skip
    for c in sorted(cases, key=lambda c: c.id):
        if c.id not in chosen:
            continue
        label = by_id[c.id]
        last = label.disputes[-1]
        gates = sorted({g for d in label.disputes for g in d.failed_gates})
        reference = ", ".join(
            [*(f"§7 {r}" for r in label.triggered_rules), *(POLICY_REFERENCE[g] for g in gates)]
        )
        records, intent, first = (
            real_rows[c.id]
            if isinstance(c, LockedCase)
            else (_records(c), _intent(c), c.script.first)
        )
        writer.writerow([
            c.id, c.title, c.data_source, c.language, c.path.value,
            "calibration" if c.id in split.calibration else "evaluation",
            records, intent, _conditions(c), first, label.outcome.value,
            " ".join(label.triggered_rules), " ".join(gates), last.queue or "", last.priority or "",
            last.inform_reason or "", " ".join(a for d in label.disputes for a in d.actions),
            reference or "§9 RESOLVE", "", "",
        ])  # fmt: skip
    return out.getvalue()


def mix(cases: Sequence[AnyCase], labels: list[CaseLabel], split: Split) -> dict[str, object]:
    by_id = {label.case_id: label for label in labels}
    source = {c.id: c.data_source for c in cases}

    def counts(ids: list[str]) -> dict[str, object]:
        chosen = [by_id[i] for i in ids]
        return {
            "cases": len(chosen),
            "by_source": dict(sorted(Counter(source[l.case_id] for l in chosen).items())),
            "by_language": dict(sorted(Counter(l.language for l in chosen).items())),
            "by_path": dict(sorted(Counter(l.path.value for l in chosen).items())),
            "by_outcome": dict(sorted(Counter(l.outcome.value for l in chosen).items())),
            "by_rule": dict(sorted(Counter(r for l in chosen for r in l.triggered_rules).items())),
            "by_failed_gate": dict(sorted(Counter(g for l in chosen for d in l.disputes for g in d.failed_gates).items())),
        }  # fmt: skip

    all_ids = sorted(c.id for c in cases)
    pt = {c.id for c in cases if c.language == "pt"}
    real = {c.id for c in cases if c.data_source == "real"}
    return {
        "all": counts(all_ids),
        "real": counts([i for i in all_ids if i in real]),
        "seeded": counts([i for i in all_ids if i not in real]),
        "portuguese": counts([i for i in all_ids if i in pt]),
        "calibration": counts(split.calibration),
        "evaluation": counts(split.evaluation),
        "evaluation_real": counts([i for i in split.evaluation if i in real]),
        "evaluation_seeded": counts([i for i in split.evaluation if i not in real]),
        "evaluation_portuguese": counts([i for i in split.evaluation if i in pt]),
    }


def everything() -> tuple[list[AnyCase], list[CaseLabel], Split]:
    """Seeded cases with their labels, and real cases from the lock (case keys only)."""
    seeded = load_scenarios()
    locked = load_lock()
    if locked is None:
        raise SystemExit("real_selection.lock is missing: run scripts/select_real_scenarios.py")
    labels = label_all(seeded) + [c.expected for c in locked.cases]
    cases: list[AnyCase] = [*seeded, *locked.cases]
    return cases, labels, make_split(cases, labels)


def build() -> dict[Path, str]:
    cases, labels, split = everything()
    seeded_labels = [label for label in labels if label.case_id.startswith("S")]
    return {
        DEFAULT_SCENARIOS_DIR / "labels.yaml": labels_yaml(seeded_labels, spec_version()),
        DEFAULT_SCENARIOS_DIR / "split.yaml": split_yaml(split),
        ROOT / "reports" / "m17_mix.json": json.dumps(mix(cases, labels, split), indent=2) + "\n",
    }


def review_size(cases: Sequence[AnyCase]) -> int:
    return min(len(cases), max(50, -(-len(cases) // 10)))


def real_review_rows(keys: list[str]) -> dict[str, tuple[str, str, str]]:
    """Records, intent and first message of these real cases, read-only from the database for
    the cases of config/eval_scenarios/local/. No identifier is written: the reference a
    customer quotes is shown as [reference]."""
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import Session

    from app.evaluation.real import load_real_scenarios, read_records, render_script
    from app.settings import get_settings

    settings = get_settings()
    if not settings.database_url:
        raise SystemExit("DATABASE_URL is not set: the review sample reads the real records")
    local = {c.id: c for c in load_real_scenarios()}
    missing = [key for key in keys if key not in local]
    if missing:
        raise SystemExit(f"{missing} are not in config/eval_scenarios/local/: run "
                         "scripts/select_real_scenarios.py")  # fmt: skip
    as_of = label_as_of(LABEL_BUSINESS_DATE)
    rows: dict[str, tuple[str, str, str]] = {}
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))
        with Session(bind=conn) as session:
            for key in keys:
                c = local[key]
                r = read_records(session, c)
                txn = r.transaction
                if txn is None:
                    disputed = "another customer's transaction"
                else:
                    disputed = (
                        f"{txn.transaction_type} {txn.amount} {txn.currency} "
                        f"(USD {txn.amount_usd}) {txn.merchant_name or ''} "
                        f"{txn.transaction_date:%Y-%m-%d %H:%M} {txn.transaction_status} "
                        f"fraud {txn.fraud_score}; {(as_of - txn.transaction_date).days}d before as_of"
                    )
                customer = r.customer.customer_status if r.customer else "missing"
                products = "; ".join(f"{p.product_type} {p.product_status}" for p in r.products)
                records = (
                    f"customer {customer}; {products}; disputed {disputed}; "
                    f"{len(r.candidates)} transactions in the pool; {len(r.cases)} cases"
                )
                values = c.dispute.model_dump(exclude_none=True, exclude_defaults=True)
                intent = ", ".join(f"{k}={v}" for k, v in values.items())
                first = render_script(c, txn).first.replace(c.transaction_id, "[reference]")
                rows[key] = (records, intent, first)
        conn.rollback()
    engine.dispose()
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail if any file is out of date")
    parser.add_argument("--review", action="store_true", help="also write the review sample")
    args = parser.parse_args()
    files = build()
    split_file = DEFAULT_SCENARIOS_DIR / "split.yaml"
    stale = []
    for path, content in files.items():
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                stale.append(str(path.relative_to(ROOT)))
            continue
        if path == split_file and path.exists() and path.read_text(encoding="utf-8") != content:
            raise SystemExit("the split changed: it is fixed before any tuning (policy §16.4)")
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"written {path.relative_to(ROOT)}")
    if stale:
        raise SystemExit(f"out of date: {stale}")
    if args.review:
        cases, labels, split = everything()
        chosen = review_sample(cases, labels, review_size(cases))
        real = [c.id for c in cases if isinstance(c, LockedCase) and c.id in chosen]
        content = review_csv(cases, labels, split, chosen, real_review_rows(real))
        REVIEW_FILE.parent.mkdir(exist_ok=True)
        REVIEW_FILE.write_text(content, encoding="utf-8", newline="\n")
        print(f"written {REVIEW_FILE.relative_to(ROOT)} (git-ignored: real records)")


if __name__ == "__main__":
    main()
