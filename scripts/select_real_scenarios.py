"""Select the M17 scenarios anchored in real records and label them with the policy engine.

    python scripts/select_real_scenarios.py          # write the local cases (and the lock, once)
    python scripts/select_real_scenarios.py --check  # select again and compare with the lock

Local use only, read-only: it reads the database with DATABASE_URL (the application role) in a
read-only transaction and never writes a row. Each category takes records that meet its
criteria, ordered by md5(seed || transaction_id), one case per customer; customers with any
case or card block are skipped, so state left by manual tests or M18 runs never enters the
selection. The label of each case is the engine on the real records, and the selection fails
if a label is not the one its category intends (as the scenario generator does).

The cases (customer_id, transaction_id, the final slots and a script whose placeholders are
filled from the record at run time) and their labels go to config/eval_scenarios/local/, which
is git-ignored: the dataset is privately distributed and no versioned file holds an identifier.
The repository keeps config/eval_scenarios/real_selection.lock: the criteria version, the seed,
the count per category, the expected label per case key and a SHA-256 of the selected
identifiers. The lock is written once; when a fresh selection differs from it, nothing is
written and the differences are listed without naming a record. The document number is never
read here; M18 reads it from data/raw/customers.csv when it logs in (app.evaluation.real).

Needs BUSINESS_DATE 2026-06-17, the date the labels are computed on. Run it before M18: a run
leaves cases on real customers, and the next selection would skip those customers.
"""

from __future__ import annotations

import argparse
import random
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import yaml  # noqa: E402
from sqlalchemy import Connection, create_engine, text  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.config import load_policy_config  # noqa: E402
from app.evaluation.labeler import LABEL_BUSINESS_DATE, CaseLabel  # noqa: E402
from app.evaluation.real import (  # noqa: E402
    CATEGORY,
    LOCAL_DIR,
    LOCK_FILE,
    REAL_CASES_FILE,
    REAL_LABELS_FILE,
    RealScenario,
    SelectionLock,
    description_identifies,
    label_as_of,
    label_real,
    load_lock,
    read_records,
    selection_digest,
    without_records,
)
from app.settings import get_settings  # noqa: E402
from app.storage.data_contract import CARD_PRODUCT_TYPES  # noqa: E402
from generate_scenarios import HELLO, Words, answers, first  # noqa: E402

CRITERIA_VERSION = "1.1.0"  # bump with any change to BASE, CRITERIA, OWNERS, PLAN or the slots
SEED = "20261004"
SCRIPT_SEED = 20261004

BASE = """
    SELECT t.customer_id, t.transaction_id
    FROM transactions t
    JOIN products p ON p.product_id = t.product_id
    JOIN customers c ON c.customer_id = t.customer_id
    WHERE c.customer_status = 'Active' AND p.product_status = 'Active'
      AND t.transaction_id NOT LIKE 'SEED-%'
      AND NOT (t.customer_id = ANY(:taken))
      AND NOT EXISTS (SELECT 1 FROM cases k WHERE k.customer_id = t.customer_id)
      AND NOT EXISTS (SELECT 1 FROM card_blocks b WHERE b.customer_id = t.customer_id)
      AND {criterion}
    ORDER BY md5(:seed || t.transaction_id)
    LIMIT :limit
"""
# Described in words: no other transaction of the customer within two days, so the description
# identifies one record (GATE-05 compares the date within one business day).
ALONE = """NOT EXISTS (SELECT 1 FROM transactions o WHERE o.customer_id = t.customer_id
      AND o.transaction_id <> t.transaction_id
      AND o.transaction_date BETWEEN t.transaction_date - interval '2 days'
                                 AND t.transaction_date + interval '2 days')"""
CARD_PURCHASE = "t.transaction_type = 'Purchase' AND p.product_type = ANY(:cards)"
RECENT = "t.transaction_date >= :recent AND t.transaction_date < :as_of"  # within the window
LOW_FRAUD = "coalesce(t.fraud_score, 0) < :fraud"
CRITERIA: dict[str, str] = {
    "unrecognized_t1": f"{CARD_PURCHASE} AND {RECENT} AND {ALONE} AND {LOW_FRAUD}"
    " AND t.transaction_status = 'Approved' AND t.amount_usd > 0 AND t.amount_usd <= :t1_max",
    "unrecognized_t2": f"{CARD_PURCHASE} AND {RECENT} AND {ALONE} AND {LOW_FRAUD}"
    " AND t.transaction_status = 'Approved' AND t.amount_usd > :t1_max AND t.amount_usd < :t3_min",
    "pending": f"{CARD_PURCHASE} AND {RECENT} AND {ALONE} AND t.transaction_status = 'Pending'",
    "declined": f"{CARD_PURCHASE} AND {RECENT} AND {ALONE} AND t.transaction_status = 'Declined'",
    "reversed": f"{CARD_PURCHASE} AND {RECENT} AND {ALONE} AND t.transaction_status = 'Reversed'",
    # Named by its reference: older than LATE_WINDOW_DAYS, so no description would find it.
    "outside_window": f"{CARD_PURCHASE} AND t.transaction_status = 'Approved'"
    " AND t.transaction_date >= :old_from AND t.transaction_date < :old_to",
    "late_filing": f"{CARD_PURCHASE} AND {ALONE} AND {LOW_FRAUD} AND t.transaction_status = 'Approved'"
    " AND t.amount_usd < :t3_min AND t.transaction_date >= :late_from AND t.transaction_date < :late_to",
    # Another customer's purchase; the customer who logs in is drawn apart (OWNERS).
    "another_customer": f"{CARD_PURCHASE} AND {RECENT} AND t.transaction_status = 'Approved'",
    "not_disputable": f"t.transaction_type = 'Deposit' AND {RECENT} AND {ALONE}"
    " AND t.transaction_status = 'Approved'",
}  # fmt: skip
OWNERS = """
    SELECT c.customer_id FROM customers c
    WHERE c.customer_status = 'Active' AND c.customer_id NOT LIKE 'SEED-%'
      AND NOT (c.customer_id = ANY(:taken))
      AND NOT EXISTS (SELECT 1 FROM cases k WHERE k.customer_id = c.customer_id)
      AND NOT EXISTS (SELECT 1 FROM card_blocks b WHERE b.customer_id = c.customer_id)
      AND EXISTS (SELECT 1 FROM products p WHERE p.customer_id = c.customer_id
                  AND p.product_status = 'Active' AND p.product_type = ANY(:cards))
    ORDER BY md5(:seed || c.customer_id)
    LIMIT :limit
"""
# category, cases in Spanish, cases in Portuguese (at least a third in Portuguese)
PLAN: list[tuple[str, int, int]] = [
    ("unrecognized_t1", 3, 2),
    ("unrecognized_t2", 2, 1),
    ("pending", 1, 1),
    ("declined", 1, 1),
    ("reversed", 1, 1),
    ("outside_window", 1, 1),
    ("late_filing", 1, 1),
    ("another_customer", 1, 1),
    ("not_disputable", 1, 1),
]
TITLES = {
    "unrecognized_t1": "unrecognized card purchase, T1",
    "unrecognized_t2": "unrecognized card purchase, T2",
    "pending": "pending purchase",
    "declined": "declined purchase",
    "reversed": "reversed purchase",
    "outside_window": "older than the late window, by reference",
    "late_filing": "late filing",
    "another_customer": "another customer's transaction, by reference",
    "not_disputable": "deposit disputed as unrecognized (N)",
}
REFERENCE_TEXT = {
    "outside_window": (["la que en el estado de cuenta aparece como {reference}"],
                       ["a que aparece no extrato como {reference}"]),
    "another_customer": (["la transacción {reference}, que es de mi hermano"],
                         ["a transação {reference}, que é do meu irmão"]),
}  # fmt: skip


def parameters() -> dict[str, Any]:
    p = load_policy_config().parameters
    as_of = label_as_of(LABEL_BUSINESS_DATE)
    return {
        "seed": SEED,
        "cards": sorted(CARD_PRODUCT_TYPES),
        "as_of": as_of,
        # Margins keep every record away from the window edges (business day = date - 06:00).
        "recent": as_of - timedelta(days=p.DISPUTE_WINDOW_DAYS - 4),
        "late_from": as_of - timedelta(days=p.LATE_WINDOW_DAYS - 9),
        "late_to": as_of - timedelta(days=p.DISPUTE_WINDOW_DAYS + 11),
        "old_from": as_of - timedelta(days=366),
        "old_to": as_of - timedelta(days=p.LATE_WINDOW_DAYS + 31),
        "t1_max": p.PROVISIONAL_CREDIT_AUTO_MAX_USD,
        "t3_min": p.AUTO_INTAKE_MAX_USD,
        "fraud": p.FRAUD_SCORE_ESCALATE,
    }


def select(conn: Connection) -> list[dict[str, Any]]:
    """The cases of PLAN, in order: (category, language, customer_id, transaction_id)."""
    values = parameters()
    taken: list[str] = []
    chosen: list[dict[str, Any]] = []
    for category, spanish, portuguese in PLAN:
        count = spanish + portuguese
        rows = conn.execute(
            text(BASE.format(criterion=CRITERIA[category])),
            {**values, "taken": taken, "limit": count * 10},
        ).all()
        picked: list[tuple[str, str]] = []
        for customer_id, transaction_id in rows:
            if customer_id not in taken and len(picked) < count:
                picked.append((customer_id, transaction_id))
                taken.append(customer_id)
        if len(picked) < count:
            raise SystemExit(f"{category}: only {len(picked)} records meet the criteria")
        owners = [customer_id for customer_id, _ in picked]
        if category == "another_customer":  # the customer who logs in is someone else
            found = conn.execute(text(OWNERS), {**values, "taken": taken, "limit": count})
            owners = [str(row[0]) for row in found]
            taken += owners
        languages = ["es"] * spanish + ["pt"] * portuguese
        for (_, transaction_id), owner, language in zip(picked, owners, languages, strict=True):
            chosen.append({"category": category, "language": language,
                           "customer_id": owner, "transaction_id": transaction_id})  # fmt: skip
    return chosen


def specification(number: int, picked: dict[str, Any], rng: random.Random) -> RealScenario:
    category = CATEGORY[picked["category"]]
    w = Words(picked["language"], random.Random(rng.random()))
    # Every case gives the same final slots, so its script answers whatever the assistant asks
    # (1.1.0: in M18 run 1 the cases that do not resolve had no answer to the card block offer).
    dispute: dict[str, Any] = {"reason_code": "RC_UNRECOGNIZED", "card_in_possession": True,
                               "shared_credentials": False, "block": "declined"}  # fmt: skip
    if category.reference == "id":
        txn_text = w.pick(*REFERENCE_TEXT[category.name])
    else:
        txn_text = "{transaction}"
    script = {
        "first": first(w, *HELLO, txn_text=txn_text, reason="RC_UNRECOGNIZED"),
        "answers": answers(w, dispute, txn_text, block=dispute.get("block")),
    }
    return RealScenario.model_validate(
        {
            "id": f"R{number:03d}",
            "title": TITLES[category.name],
            "category": category.name,
            "language": picked["language"],
            "path": category.path.value,
            "data_source": "real",
            "customer_id": picked["customer_id"],
            "transaction_id": picked["transaction_id"],
            "reference": category.reference,
            "dispute": dispute,
            "script": script,
        }
    )


def check_label(scenario: RealScenario, label: CaseLabel) -> None:
    category = CATEGORY[scenario.category]
    last = label.disputes[-1]
    tier = last.tier if category.tier is not None else None  # checked where the category names one
    got = (label.outcome, tuple(label.triggered_rules), tier, last.inform_reason)
    want = (category.outcome, category.rules, category.tier, category.inform_reason)
    if got != want:
        raise SystemExit(f"{scenario.id} {scenario.category}: labeled {got}, intended {want}")


def selection(conn: Connection) -> tuple[list[RealScenario], list[CaseLabel]]:
    """The selected cases and their labels: the engine on the real records."""
    picked = select(conn)
    rng = random.Random(SCRIPT_SEED)
    scenarios = [specification(n, p, rng) for n, p in enumerate(picked, start=1)]
    config = load_policy_config()
    labels: list[CaseLabel] = []
    with Session(bind=conn) as session:
        for scenario in scenarios:
            records = read_records(session, scenario, config)
            if scenario.reference == "description" and not (
                records.transaction is not None
                and description_identifies(records.transaction, records.candidates, config)
            ):
                raise SystemExit(f"{scenario.id}: its description does not identify one record")
            label = label_real(scenario, records, config)
            check_label(scenario, label)
            labels.append(label)
    return scenarios, labels


def lock(scenarios: list[RealScenario], labels: list[CaseLabel]) -> SelectionLock:
    by_id = {label.case_id: label for label in labels}
    counts = {category: {"es": spanish, "pt": portuguese} for category, spanish, portuguese in PLAN}
    return SelectionLock.model_validate(
        {
            "criteria_version": CRITERIA_VERSION,
            "seed": SEED,
            "business_date": LABEL_BUSINESS_DATE,
            "counts": counts,
            "ids_sha256": selection_digest(scenarios),
            "cases": [
                {"id": s.id, "category": s.category, "language": s.language,
                 "path": s.path, "expected": without_records(by_id[s.id])}  # fmt: skip
                for s in scenarios
            ],
        }
    )


def lock_text(locked: SelectionLock) -> str:
    header = (
        "# M17 cases on real records: what the repository keeps of the selection\n"
        "# (scripts/select_real_scenarios.py). No identifier: ids_sha256 is the SHA-256 of the\n"
        "# lines '<case key> <customer_id> <transaction_id>' ordered by key; --check selects again\n"
        "# and compares. The cases themselves are in config/eval_scenarios/local/ (git-ignored).\n"
    )
    body = locked.model_dump(mode="json")
    return header + yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100)


def differences(old: SelectionLock, new: SelectionLock) -> list[str]:
    """What changed between the fixed lock and a fresh selection, without naming a record."""
    found = [
        f"{name}: {getattr(old, name)} -> {getattr(new, name)}"
        for name in ("criteria_version", "seed", "business_date", "counts")
        if getattr(old, name) != getattr(new, name)
    ]
    if old.ids_sha256 != new.ids_sha256:
        found.append("the selected identifiers changed (ids_sha256)")
    before = {c.id: c for c in old.cases}
    after = {c.id: c for c in new.cases}
    for key in sorted(before.keys() | after.keys()):
        if before.get(key) != after.get(key):
            found.append(f"{key}: the case or its expected label changed")
    return found


def local_files(scenarios: list[RealScenario], labels: list[CaseLabel]) -> dict[Path, str]:
    cases = [s.model_dump(mode="json", exclude_defaults=True, exclude_none=True) for s in scenarios]
    header = (
        f"# M17 scenarios on real records ({len(cases)} cases), selected read-only with seed {SEED}.\n"
        "# LOCAL ONLY (git-ignored): real identifiers never enter the repository. The records are\n"
        "# read at run time and the document number is never stored. {transaction} and\n"
        "# {reference} are filled from the record at run time (app.evaluation.real.render_script).\n"
    )
    labels_header = (
        "# LOCAL ONLY (git-ignored). Reference labels of the real cases: the policy engine on the\n"
        "# real records (scripts/select_real_scenarios.py, business date 2026-06-17).\n"
    )

    def dump(body: dict[str, Any]) -> str:
        return yaml.safe_dump(body, allow_unicode=True, sort_keys=False, width=100)

    return {
        REAL_CASES_FILE: header + dump({"cases": cases}),
        REAL_LABELS_FILE: labels_header
        + dump({"labels": [label.model_dump(mode="json") for label in labels]}),
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--check", action="store_true", help="fail if the selection differs")
    args = parser.parse_args()
    settings = get_settings()
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    if settings.business_date != LABEL_BUSINESS_DATE:
        sys.exit(f"BUSINESS_DATE must be {LABEL_BUSINESS_DATE}: the labels are computed on it")
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("SET TRANSACTION READ ONLY"))  # never writes a row
        scenarios, labels = selection(conn)
        conn.rollback()
    engine.dispose()
    fresh = lock(scenarios, labels)
    fixed = load_lock()
    changed = differences(fixed, fresh) if fixed is not None else []
    if changed:
        raise SystemExit(
            "the selection differs from real_selection.lock (nothing written):\n  "
            + "\n  ".join(changed)
        )
    if args.check:
        if fixed is None:
            raise SystemExit("real_selection.lock is missing")
        print("the selection matches real_selection.lock")
        return
    LOCAL_DIR.mkdir(exist_ok=True)
    for path, content in local_files(scenarios, labels).items():
        path.write_text(content, encoding="utf-8", newline="\n")
        print(f"written {path.relative_to(ROOT)} (git-ignored)")
    if fixed is None:
        LOCK_FILE.write_text(lock_text(fresh), encoding="utf-8", newline="\n")
        print(f"written {LOCK_FILE.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
