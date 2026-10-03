"""Summarize the M17 human review (policy §16.3) into reports/m17_review_summary.json.

    python scripts/summarize_review.py

Reads the reviewer's verdicts from reports/m17_review/review_sample.csv (git-ignored: it shows
real records) and writes aggregates and case keys only: how many cases were reviewed, by data
source, language and path; how many labels were confirmed; each discrepancy with its case key,
reason and resolution. It also lists what earlier review rounds found and how it was resolved
before the final verdicts. No customer or transaction identifier and no record value is
written. Refuses while a sampled case has no verdict.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLE = ROOT / "reports" / "m17_review" / "review_sample.csv"
SUMMARY = ROOT / "reports" / "m17_review_summary.json"
CONFIRMED = {"OK", "CONFIRMED", "CONFIRMADA", "CONFIRMADO"}
IDENTIFIER = re.compile(r"\b(?:CLI|PRD|TRX|CMP)-[A-Za-z0-9-]{6,}\b|\b[0-9a-f]{64}\b")

# What the review rounds before the final verdicts found, and how each finding was resolved.
EARLIER_ROUNDS = [
    {"cases": ["S076"], "finding": "label: three unrecognized charges reported in the first message should escalate under ESC-03 before any case is created",
     "resolution": "policy 0.4.11 (the ESC-03 batch counts reported charges), extract@1.11.0 unrecognized_reported, Orchestrator and labeler; S076 relabeled", "label_changed": True},
    {"cases": ["S013"], "finding": "extraction: a stolen card was read as account takeover",
     "resolution": "extract prompt states that a lost or stolen card is not account_takeover_reported (extract@1.11.0)", "label_changed": False},
    {"cases": ["S023", "S024", "S032", "S064"], "finding": "script: currency not matching the customer's country",
     "resolution": "generator: every record in its country's currency (COP, ARS, USD), checked by the generator and a test", "label_changed": False},
    {"cases": ["S069", "S078", "S085"], "finding": "script: Portuguese preposition and article not contracted",
     "resolution": "generator contracts em/de with the article; a test rejects em o, em a, de o, de a", "label_changed": False},
    {"cases": ["S065"], "finding": "script: merchant not credible for a USD 1,450 purchase",
     "resolution": "T3 purchases at Electro Mundo", "label_changed": False},
    {"cases": ["S072", "S073"], "finding": "script: duplicated or badly joined text in Portuguese",
     "resolution": "openings rewritten; a test rejects repeated words in a row", "label_changed": False},
    {"cases": ["S083"], "finding": "script: Procon is a Brazilian body", "resolution": "neutral wording (consumer protection body)", "label_changed": False},
    {"cases": ["S054"], "finding": "review sheet: policy reference pointed to §9 RESOLVE",
     "resolution": "reference to §8 COM-03 (withdrawn at the confirmation)", "label_changed": False},
]  # fmt: skip


def scrub(text: str) -> str:
    return IDENTIFIER.sub("[ref]", text)


def summarize(rows: list[dict[str, str]]) -> dict[str, object]:
    pending = [r["case_id"] for r in rows if not r["reviewer_verdict"].strip()]
    if pending:
        raise SystemExit(f"cases without a verdict: {pending}")
    confirmed = [r for r in rows if r["reviewer_verdict"].strip().upper() in CONFIRMED]
    discrepancies = [
        {"case_key": r["case_id"], "verdict": r["reviewer_verdict"].strip(),
         "reason": scrub(r["reviewer_notes"].strip()), "resolution": "pending"}
        for r in rows if r not in confirmed
    ]  # fmt: skip
    return {
        "policy_section": "§16.3",
        "description": "Human review of the M17 reference labels: aggregates and case keys only.",
        "reviewed": len(rows),
        "by_data_source": dict(sorted(Counter(r["data_source"] for r in rows).items())),
        "by_language": dict(sorted(Counter(r["language"] for r in rows).items())),
        "by_path": dict(sorted(Counter(r["path"] for r in rows).items())),
        "always_reviewed_rules": ["ESC-03", "ESC-06", "ESC-13"],
        "confirmed": len(confirmed),
        "discrepancies": discrepancies,
        "earlier_review_rounds": EARLIER_ROUNDS,
    }


def main() -> None:
    if not SAMPLE.exists():
        sys.exit(f"{SAMPLE.relative_to(ROOT)} is missing: run scripts/label_scenarios.py --review")
    with SAMPLE.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    summary = summarize(rows)
    SUMMARY.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    print(f"written {SUMMARY.relative_to(ROOT)}: {summary['confirmed']} of {summary['reviewed']} confirmed")


if __name__ == "__main__":
    main()
