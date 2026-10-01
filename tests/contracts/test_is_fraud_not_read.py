"""``is_fraud`` is a calibration label only (policy §15): it is never read at runtime.

It is not in the records the Tool Layer returns, not in the Policy Engine's input, not in the
LLM or Kev context, and no runtime module mentions it. Only the storage schema (which mirrors
the Core Banking table) and the ingestion pipeline (which loads it) know the column.
"""

from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

import app.contracts as contracts
from app.contracts import (
    CaseRecord,
    CustomerRecord,
    LLMContext,
    PolicyDecision,
    PolicyRequest,
    ProductRecord,
    TraceRecord,
    TransactionRecord,
)

APP = Path(__file__).resolve().parents[2] / "app"
ALLOWED = {APP / "storage" / "models.py", APP / "ingestion" / "core.py"}


def _field_names(model: type[BaseModel], seen: set[type[BaseModel]] | None = None) -> set[str]:
    """Every field name in the model and the models nested in it."""
    seen = seen if seen is not None else set()
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for nested in re.findall(r"\w+", repr(field.annotation)):
            candidate = getattr(contracts, nested, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate, seen)
    return names


def test_no_runtime_contract_carries_is_fraud() -> None:
    for model in (
        TransactionRecord,
        CustomerRecord,
        ProductRecord,
        CaseRecord,
        PolicyRequest,
        PolicyDecision,
        LLMContext,
        TraceRecord,
    ):
        assert "is_fraud" not in _field_names(model), model.__name__


def test_nested_fields_are_inspected() -> None:
    # Guard for the helper itself: PolicyRequest reaches TransactionRecord's fields.
    assert "fraud_score" in _field_names(PolicyRequest)


def test_only_storage_schema_and_ingestion_mention_is_fraud() -> None:
    mentions = {
        path for path in APP.rglob("*.py") if "is_fraud" in path.read_text(encoding="utf-8")
    }
    assert mentions == ALLOWED
