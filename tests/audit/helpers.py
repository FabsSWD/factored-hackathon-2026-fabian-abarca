"""Turn traces for audit tests: every stage filled, from real engine decisions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.contracts import (
    ActionId,
    Confirmation,
    InputGuardResult,
    Language,
    ModelCall,
    ModelSignals,
    ModelSource,
    Outcome,
    PolicyDecision,
    ToolResult,
    ToolStatus,
    TraceRecord,
)
from tests.policy.conftest import evaluate, request, slots

START = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def model_calls() -> list[ModelCall]:
    return [
        ModelCall(
            provider="openai",
            model="gpt-6-luna",
            response_model="gpt-6-luna",
            prompt_version="extract@1.6.0",
            prompt_hash="sha256:0123456789abcdef",
            purpose="extract_slots",
            input_tokens=1340,
            output_tokens=120,
            latency_ms=4600.0,
            success=True,
        ),
        ModelCall(
            provider="kev",
            model="kev-latest",
            prompt_version="kev_questions@1.0.0",
            purpose="decision_signals",
            input_tokens=167,
            output_tokens=163,
            latency_ms=300.0,
            success=True,
        ),
    ]


def resolved_decision() -> PolicyDecision:
    return evaluate(request(slots=slots(confirmation=Confirmation.CONFIRMED)))


def case_created() -> ToolResult:
    return ToolResult(
        action=ActionId.CREATE_CASE,
        status=ToolStatus.SUCCESS,
        verified=True,
        attempts=1,
        idempotency_key="TXN-1:RC_INCORRECT_AMOUNT",
        record_id="DSP-20261001-000001",
        completed_at=START,
    )


def trace(
    trace_id: str = "TRC-1",
    *,
    conversation_id: str = "CONV-1",
    turn_index: int = 0,
    minutes: float = 0,
    **values: Any,
) -> TraceRecord:
    fields: dict[str, Any] = {
        "trace_id": trace_id,
        "conversation_id": conversation_id,
        "session_id": None,
        "turn_index": turn_index,
        "created_at": START + timedelta(minutes=minutes),
        "language": Language.ES,
        "message": "Sí, confirmo. Mi documento es X1234567 y mi correo ana@example.test",
        "input_guard": InputGuardResult(flagged=False, strikes=0),
        "model_calls": model_calls(),
        "signals": ModelSignals(source=ModelSource.LLM_FALLBACK),
        "decisions": [resolved_decision()],
        "tool_calls": [case_created()],
        "outcome": Outcome.RESOLVE,
        "handoff_id": None,
        "stage_latencies_ms": {"extract": 4600.0, "policy": 2.0, "tools": 80.0},
        "total_latency_ms": 4800.0,
        "estimated_cost_usd": Decimal("0.004"),
        "policy_version": "0.4.2",
        "error": None,
    }
    fields.update(values)
    return TraceRecord(**fields)
