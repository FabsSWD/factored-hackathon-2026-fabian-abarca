"""Metrics over turn traces (M11).

Definitions (a conversation is all traces with the same ``conversation_id``):

- Outcome of a conversation: the outcome of its last trace (highest ``turn_index``).
- Attempted case: an authenticated conversation that reached at least one transaction
  evaluation (a decision that evaluated GATE-05). The other conversations are reported apart,
  by where they stopped: ``language`` (GATE-01), ``authentication`` (GATE-02), ``account``
  (GATE-03 or GATE-04), or ``no_decision`` (no decision was evaluated, e.g. an error).
- Abandoned: a conversation whose last outcome is CLARIFY (the customer stopped answering).
- Handoff: any ESCALATE outcome or handoff in the conversation.
- Successful automated resolution: a verified, successful ACT-02 and no handoff.
- Containment: the conversation ended without a handoff, in RESOLVE or INFORM.
- Escalation rate: conversations with a handoff / all conversations, broken down by queue and
  by triggered rule (each counted once per conversation).
- Cost per attempted case = total cost / attempted cases; cost per automated resolution = total
  cost / successful resolutions, so the cost of conversations that did not resolve counts too.
- Percentiles use linear interpolation between closest ranks (the common "type 7" method).
- Language and tier of a conversation: the last known value ("unknown" if none).

Unsafe outcomes (an improper RESOLVE, a missed escalation) and the quality of escalations need
reference labels: they are computed in M18 on the scenario set of M17, not from traces alone.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Sequence
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from app.contracts import ActionId, Outcome, PolicyDecision, ToolStatus, TraceRecord

UNKNOWN = "unknown"
_STOPPED_AT = {"GATE-01": "language", "GATE-02": "authentication", "GATE-03": "account",
               "GATE-04": "account"}  # fmt: skip


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
    attempted_cases: int
    ended_before_transaction: dict[str, int]  # language, authentication, account, no_decision
    abandoned: int
    automated_resolutions: int
    contained: int
    containment_rate: float | None
    escalated: int
    escalation_rate: float | None
    escalations_by_queue: dict[str, int]
    escalations_by_rule: dict[str, int]
    turn_latency: LatencySummary
    stage_latency: dict[str, LatencySummary]
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


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def _reached_transaction(decision: PolicyDecision) -> bool:
    return any(gate.gate_id == "GATE-05" for gate in decision.gates_evaluated)


def compute_metrics(traces: Sequence[TraceRecord]) -> AuditMetrics:
    conversations: dict[str, list[TraceRecord]] = defaultdict(list)
    for trace in traces:
        conversations[trace.conversation_id].append(trace)

    outcomes: Counter[str] = Counter()
    ended_before: Counter[str] = Counter()
    by_language: dict[str, Counter[str]] = defaultdict(Counter)
    by_tier: dict[str, Counter[str]] = defaultdict(Counter)
    by_queue: Counter[str] = Counter()
    by_rule: Counter[str] = Counter()
    attempted = abandoned = resolved = contained = escalated = 0
    for turns in conversations.values():
        turns.sort(key=lambda t: (t.turn_index, t.created_at))
        final_outcome = turns[-1].outcome
        final = final_outcome.value if final_outcome else UNKNOWN
        outcomes[final] += 1
        decisions = [d for t in turns for d in t.decisions]
        language = next((t.language.value for t in reversed(turns) if t.language), UNKNOWN)
        tier = next((d.tier.value for d in reversed(decisions) if d.tier), UNKNOWN)
        by_language[language][final] += 1
        by_tier[tier][final] += 1

        if any(_reached_transaction(d) for d in decisions):
            attempted += 1
        elif decisions and decisions[-1].gates_evaluated:
            last_gate = decisions[-1].gates_evaluated[-1].gate_id
            ended_before[_STOPPED_AT.get(last_gate, "account")] += 1
        else:
            ended_before["no_decision"] += 1
        abandoned += final_outcome is Outcome.CLARIFY

        escalations = [d for d in decisions if d.outcome is Outcome.ESCALATE]
        handoff = bool(escalations) or any(
            t.handoff_id is not None or t.outcome is Outcome.ESCALATE for t in turns
        )
        if handoff:
            escalated += 1
            by_queue.update({d.queue.value for d in escalations if d.queue})
            by_rule.update({rule for d in escalations for rule in d.triggered_rules})
        created = any(
            call.action is ActionId.CREATE_CASE
            and call.status is ToolStatus.SUCCESS
            and call.verified
            for t in turns
            for call in t.tool_calls
        )
        resolved += created and not handoff
        contained += not handoff and final_outcome in (Outcome.RESOLVE, Outcome.INFORM)

    costs = [t.estimated_cost_usd for t in traces if t.estimated_cost_usd is not None]
    total_cost = sum(costs, Decimal(0)) if costs else None
    stages: dict[str, list[float]] = defaultdict(list)
    for trace in traces:
        for stage, value in trace.stage_latencies_ms.items():
            stages[stage].append(value)
    total = len(conversations)
    return AuditMetrics(
        turns=len(traces),
        conversations=total,
        outcomes=dict(outcomes),
        attempted_cases=attempted,
        ended_before_transaction=dict(sorted(ended_before.items())),
        abandoned=abandoned,
        automated_resolutions=resolved,
        contained=contained,
        containment_rate=_rate(contained, total),
        escalated=escalated,
        escalation_rate=_rate(escalated, total),
        escalations_by_queue=dict(sorted(by_queue.items())),
        escalations_by_rule=dict(sorted(by_rule.items())),
        turn_latency=_summary(
            [t.total_latency_ms for t in traces if t.total_latency_ms is not None]
        ),
        stage_latency={stage: _summary(values) for stage, values in sorted(stages.items())},
        total_cost_usd=total_cost,
        cost_per_attempted_case_usd=_per(total_cost, attempted),
        cost_per_automated_resolution_usd=_per(total_cost, resolved),
        turns_without_cost=len(traces) - len(costs),
        outcomes_by_language={k: dict(v) for k, v in sorted(by_language.items())},
        outcomes_by_tier={k: dict(v) for k, v in sorted(by_tier.items())},
    )
