"""Versioned reports carry aggregate figures only: no customer, product or transaction
identifier and no document hash may appear in them.

Column names such as "customer_id" are allowed (quality reports list the schema); values
that look like identifiers are not.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

REPORTS = Path(__file__).resolve().parent.parent / "reports"

IDENTIFIER = re.compile(
    r"\b(?:CLI-[A-Z0-9]{8,}|PRD-[A-Z0-9]{8,}|TRX-[A-Z0-9]{8,}|CMP-[A-Z0-9]{8,})\b"
    r"|\b[0-9a-f]{64}\b"
)


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def identifiers_in(report: object) -> list[str]:
    return [match.group(0) for text in _strings(report) for match in IDENTIFIER.finditer(text)]


REPORT_FILES = sorted(REPORTS.glob("*.json"))


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
