"""The learned component (M18): Kev against the fallback derived from ``extract`` (M6).

Both sources are scored on the same turns, from what the harness captured: Kev's signals and
``derive_fallback`` of the turn's extraction. The truth of each question comes from the case,
never from the bank's escalation labels (which carry no signal):

- ``reason_code``: the reason of the dispute in progress, on the turns where the customer
  states it (the first message and the answer to ``clarify:reason_code``). Accuracy of the
  most likely code (the "other" mass counts as a wrong answer), coverage, multi-class Brier
  and the expected calibration error (ECE) of the top probability.
- ``ambiguous``: the turn's decision asked for the transaction or the reason (1) or not (0).
- ``escalation_risk``: the case's label is ESCALATE (1) or not (0).

Brier is the mean squared error of the probability; ECE uses ten equal-width bins. The
ESC-11 thresholds (``DECISION_CONFIDENCE_MIN``, ``ESCALATION_RISK_THRESHOLD``) stay null:
these numbers describe Kev as served, uncalibrated.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from app.contracts import ModelSignals, ModelSource, ReasonCode

REASONS = tuple(ReasonCode)
BINS = 10


@dataclass(frozen=True)
class SignalTurn:
    source: str  # "kev" | "llm_fallback"
    signals: ModelSignals
    reason: ReasonCode | None  # truth on a reason turn, else None
    ambiguous: int  # 0/1 truth
    escalate: int  # 0/1 truth


def ece(pairs: Sequence[tuple[float, int]], bins: int = BINS) -> float | None:
    """Expected calibration error of (probability, outcome) pairs, equal-width bins."""
    if not pairs:
        return None
    total = 0.0
    for b in range(bins):
        low, high = b / bins, (b + 1) / bins
        group = [(p, y) for p, y in pairs if low <= p < high or (b == bins - 1 and p == 1.0)]
        if group:
            confidence = sum(p for p, _ in group) / len(group)
            accuracy = sum(y for _, y in group) / len(group)
            total += len(group) / len(pairs) * abs(confidence - accuracy)
    return total


def brier(pairs: Sequence[tuple[float, int]]) -> float | None:
    if not pairs:
        return None
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs)


def _reason_metrics(turns: Sequence[SignalTurn]) -> dict[str, object]:
    asked = [t for t in turns if t.reason is not None]
    predicted = [t for t in asked if t.signals.reason_code_probs]
    hits, confidences, squared = 0, [], []
    for t in predicted:
        probs = t.signals.reason_code_probs
        other = t.signals.reason_code_other or 0.0
        best = max(probs, key=lambda code: probs[code])
        top = probs[best]
        correct = top > other and best == t.reason
        hits += correct
        confidences.append((max(top, other), int(correct)))
        error = sum((probs.get(code, 0.0) - (code == t.reason)) ** 2 for code in REASONS)
        squared.append(error + other**2)
    return {
        "turns": len(asked),
        "coverage": len(predicted) / len(asked) if asked else None,
        "accuracy": hits / len(predicted) if predicted else None,
        "brier": sum(squared) / len(squared) if squared else None,
        "ece": ece(confidences),
    }


def _binary(turns: Sequence[SignalTurn], name: str) -> dict[str, object]:
    pairs = []
    for t in turns:
        value = t.signals.ambiguity if name == "ambiguous" else t.signals.escalation_risk
        if value is not None:
            pairs.append((value, t.ambiguous if name == "ambiguous" else t.escalate))
    return {"turns": len(pairs), "brier": brier(pairs), "ece": ece(pairs)}


def compare(turns: Sequence[SignalTurn]) -> dict[str, object]:
    """Per source and question. Kev appears only if it answered at least once."""
    out: dict[str, object] = {}
    for source in (ModelSource.KEV.value, ModelSource.LLM_FALLBACK.value):
        mine = [t for t in turns if t.source == source]
        out[source] = (
            {
                "turns": len(mine),
                "reason_code": _reason_metrics(mine),
                "ambiguous": _binary(mine, "ambiguous"),
                "escalation_risk": _binary(mine, "escalation_risk"),
            }
            if mine
            else None
        )
    out["esc11_thresholds"] = {
        "DECISION_CONFIDENCE_MIN": None,
        "ESCALATION_RISK_THRESHOLD": None,
        "note": "uncalibrated: null until M7 fits them on the calibration split",
    }
    return out
