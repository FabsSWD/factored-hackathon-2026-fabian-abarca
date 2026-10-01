"""Evidence of fired rules: which input made each rule fire, and where it came from.

Every piece is copied from the request (slots, flags, counters, records, signals, tool results),
never generated. The handoff packet shows it as ``escalation_reasons`` (policy §13).
"""

from __future__ import annotations

from app.contracts import Evidence, EvidenceKind, TransactionRecord

CLAIM = "customer statement (LLM extraction)"
FLAG_ORIGINS = {
    # app/llm_adapter/signals.py: these two never depend on the model alone.
    "human_requested": "customer statement (rule detector and LLM extraction)",
    "legal_or_vulnerability": "customer statement (rule detector and LLM extraction)",
    "account_takeover_reported": "customer statement (LLM extraction)",
}
COUNTER = "conversation counter"
CORE_BANKING = "Core Banking"
CASES = "Cases"


def slot(name: str, value: object) -> Evidence:
    text = ("yes" if value else "no") if isinstance(value, bool) else str(value)
    return Evidence(kind=EvidenceKind.SLOT, name=name, value=text, origin=CLAIM)


def flag(name: str) -> Evidence:
    return Evidence(kind=EvidenceKind.FLAG, name=name, value="true", origin=FLAG_ORIGINS[name])


def counter(name: str, value: object) -> Evidence:
    return Evidence(kind=EvidenceKind.COUNTER, name=name, value=str(value), origin=COUNTER)


def record(
    name: str, value: object, source: str, record_id: str | None, origin: str = CORE_BANKING
) -> Evidence:
    return Evidence(
        kind=EvidenceKind.RECORD,
        name=name,
        value=str(value),
        origin=origin,
        source=source,
        record_id=record_id,
    )


def transaction(txn: TransactionRecord, name: str, value: object) -> Evidence:
    return record(name, value, "transactions", txn.transaction_id)


def other(kind: EvidenceKind, name: str, value: object, origin: str) -> Evidence:
    return Evidence(kind=kind, name=name, value=str(value), origin=origin)
