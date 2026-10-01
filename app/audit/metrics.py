"""Metrics over turn traces (M11): outcomes, latency percentiles, cost, by language and tier.

Definitions (conversation = all traces with the same ``conversation_id``):

- The outcome of a conversation is the outcome of its last trace (highest ``turn_index``).
- An attempted case is a conversation where the Policy Engine evaluated at least one decision.
- A successful automated resolution is a conversation with a verified, successful ACT-02.
- Cost per attempted case = total cost / attempted cases; cost per automated resolution = total
  cost / successful resolutions, so the cost of conversations that did not resolve counts too.
- Percentiles use linear interpolation between closest ranks (the common "type 7" method).
- Language and tier of a conversation: the last known value in its traces ("unknown" if none).
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ActionId, ToolStatus, TraceRecord

UNKNOWN = "unknown"


def percentile(values: Sequence[float], q: float) -> float | None:
    """The ``q`` quantile (0..1) by linear interpolation; ``None`` for no values."""
    if not 0 <= q <= 1:
        raise ValueError("q must be between 0 and 1")
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


class LatencySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    count: int
    p50_ms: float | None
    p95_ms: float | None


class AuditMetrics(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    turns: int
    conversations: int
    outcomes: dict[str, int]  # final outcome per conversation
    turn_latency: LatencySummary
    stage_latency: dict[str, LatencySummary]
    attempted_cases: int
    automated_resolutions: int
    total_cost_usd: Decimal | None
    cost_per_attempted_case_usd: Decimal | None
    cost_per_automated_resolution_usd: Decimal | None
    # Turns whose cost is unknown (no token rates); the totals cover only the other turns.
    turns_without_cost: int
    outcomes_by_language: dict[str, dict[str, int]] = Field(default_factory=dict)
    outcomes_by_tier: dict[str, dict[str, int]] = Field(default_factory=dict)


def _summary(values: Sequence[float]) -> LatencySummary:
    return LatencySummary(
        count=len(values), p50_ms=percentile(values, 0.5), p95_ms=percentile(values, 0.95)
    )


def _per(total: Decimal | None, count: int) -> Decimal | None:
    if total is None or count == 0:
        return None
    return (total / count).quantize(Decimal("0.000001"))


def compute_metrics(traces: Sequence[TraceRecord]) -> AuditMetrics:
    conversations: dict[str, list[TraceRecord]] = defaultdict(list)
    for trace in traces:
        conversations[trace.conversation_id].append(trace)

    outcomes: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = defaultdict(Counter)
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    attempted = resolved = 0
    for turns in conversations.values():
        turns.sort(key=lambda t: (t.turn_index, t.created_at))
        final = turns[-1].outcome.value if turns[-1].outcome else UNKNOWN
        outcomes[final] += 1
        language = next((t.language.value for t in reversed(turns) if t.language), UNKNOWN)
        decisions = [d for t in turns for d in t.decisions]
        tier = next((d.tier.value for d in reversed(decisions) if d.tier), UNKNOWN)
        by_language[language][final] += 1
        by_tier[tier][final] += 1
        attempted += bool(decisions)
        resolved += any(
            call.action is ActionId.CREATE_CASE
            and call.status is ToolStatus.SUCCESS
            and call.verified
            for t in turns
            for call in t.tool_calls
        )

    costs = [t.estimated_cost_usd for t in traces if t.estimated_cost_usd is not None]
    total_cost = sum(costs, Decimal(0)) if costs else None
    stages: dict[str, list[float]] = defaultdict(list)
    for trace in traces:
        for stage, value in trace.stage_latencies_ms.items():
            stages[stage].append(value)
    return AuditMetrics(
        turns=len(traces),
        conversations=len(conversations),
        outcomes=dict(outcomes),
        turn_latency=_summary(
            [t.total_latency_ms for t in traces if t.total_latency_ms is not None]
        ),
        stage_latency={stage: _summary(values) for stage, values in sorted(stages.items())},
        attempted_cases=attempted,
        automated_resolutions=resolved,
        total_cost_usd=total_cost,
        cost_per_attempted_case_usd=_per(total_cost, attempted),
        cost_per_automated_resolution_usd=_per(total_cost, resolved),
        turns_without_cost=len(traces) - len(costs),
        outcomes_by_language={k: dict(v) for k, v in sorted(by_language.items())},
        outcomes_by_tier={k: dict(v) for k, v in sorted(by_tier.items())},
    )
