"""Versioned reports carry aggregate figures only: no customer, product or transaction
identifier and no document hash may appear in them.

Column names such as "customer_id" are allowed (quality reports list the schema); values
that look like identifiers are not.

The same rule holds for the M17 evaluation files in config/eval_scenarios/, except local/
(git-ignored), where the cases on real records keep their identifiers. Two keys there hold a
SHA-256 that is not a document hash: the split's ``fingerprint`` and the lock's ``ids_sha256``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
REPORTS = ROOT / "reports"
EVAL_SCENARIOS = ROOT / "config" / "eval_scenarios"
DIGEST_KEYS = frozenset({"fingerprint", "ids_sha256"})  # digests of the split and the selection

IDENTIFIER = re.compile(
    r"\b(?:CLI-[A-Z0-9]{8,}|PRD-[A-Z0-9]{8,}|TRX-[A-Z0-9]{8,}|CMP-[A-Z0-9]{8,})\b"
    r"|\b[0-9a-f]{64}\b"
)


def _strings(value: object, digests: frozenset[str] = frozenset()) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            if key not in digests:
                yield from _strings(item, digests)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item, digests)
    elif value is not None:
        yield str(value)


def identifiers_in(report: object, digests: frozenset[str] = frozenset()) -> list[str]:
    return [
        match.group(0) for text in _strings(report, digests) for match in IDENTIFIER.finditer(text)
    ]


REPORT_FILES = sorted(REPORTS.glob("*.json"))
EVAL_FILES = sorted(
    path
    for path in EVAL_SCENARIOS.rglob("*")
    if path.is_file() and "local" not in path.relative_to(EVAL_SCENARIOS).parts
)


def test_reports_exist() -> None:
    assert REPORT_FILES, "reports/*.json are versioned and must be present"


@pytest.mark.parametrize("path", REPORT_FILES, ids=lambda p: p.name)
def test_report_contains_no_identifiers(path: Path) -> None:
    found = identifiers_in(json.loads(path.read_text(encoding="utf-8")))
    assert found == [], f"{path.name} contains identifiers: {found[:5]}"


@pytest.mark.parametrize(
    "leak",
    [
        {"sample": "CLI-G4X2AMVD62NR"},
        {"ids": ["PRD-LT0TPC5YC33U"]},
        {"TRX-UIWYPTP5S7PTBYJUSELQ": 1},
        {"hash": "a" * 64},
        {"text": "rejected CMP-J7LT0TPC5YC33ULTQJZD"},
    ],
)
def test_checker_detects_identifiers(leak: object) -> None:
    assert identifiers_in(leak)


def test_checker_allows_column_names_and_figures() -> None:
    clean = {"column_types": {"customer_id": "VARCHAR", "document_hash": "VARCHAR"}, "rows": 42}
    assert identifiers_in(clean) == []


def test_eval_files_exist() -> None:
    names = {path.name for path in EVAL_FILES}
    assert {"split.yaml", "labels.yaml", "real_selection.lock"} <= names


@pytest.mark.parametrize("path", EVAL_FILES, ids=lambda p: p.name)
def test_eval_file_contains_no_identifiers(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    comments = [line for line in text.splitlines() if line.lstrip().startswith("#")]
    found = identifiers_in(yaml.safe_load(text), DIGEST_KEYS) + identifiers_in(comments)
    assert found == [], f"{path.name} contains identifiers: {found[:5]}"


def test_only_the_digest_keys_may_hold_a_sha256() -> None:
    assert identifiers_in({"fingerprint": "a" * 64, "ids_sha256": "b" * 64}, DIGEST_KEYS) == []
    assert identifiers_in({"other": "a" * 64}, DIGEST_KEYS)
    assert identifiers_in({"cases": [{"customer_id": "CLI-ETG3VM0X7UTD"}]}, DIGEST_KEYS)
