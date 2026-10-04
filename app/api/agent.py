"""Agent console API (M12, M15): the queue of escalated cases, each handoff packet, the turn
traces as a searchable list, and the operating metrics. Agent role only, with the same token and
access log as the audit API. Lists are paged: each page says how many items match in total."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.audit import AgentTracer, Filters
from app.audit.metrics import AuditMetrics
from app.audit.tracer import TraceFilter, TracePage
from app.contracts import HandoffPacket, Language, Outcome, Priority, Queue
from app.handoff.queue import DatabaseHandoffQueue, HandoffFilter, HandoffPage

router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_queue(request: Request) -> DatabaseHandoffQueue:
    queue: DatabaseHandoffQueue | None = getattr(request.app.state, "handoff_queue", None)
    if queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "agent_console_unavailable")
    return queue


HandoffQueue = Annotated[DatabaseHandoffQueue, Depends(get_queue)]


@router.get("/session", status_code=status.HTTP_204_NO_CONTENT)
def check_session(tracer: AgentTracer) -> None:
    """204 if the token proves the agent role, 403 otherwise: the console's sign-in check. Recorded
    like any other read."""


@router.get("/handoffs")
def list_handoffs(
    tracer: AgentTracer,
    handoffs: HandoffQueue,
    queue: Queue | None = None,
    priority: Priority | None = None,
    since: datetime | None = None,
    search: Annotated[str | None, Query(max_length=64)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> HandoffPage:
    """Escalated cases, high priority first, then the oldest. ``search`` matches part of the
    handoff ID the customer was given (HO-...), the conversation ID, the customer reference or the
    request summary."""
    return handoffs.page(
        HandoffFilter(
            queue=queue,
            priority=priority,
            since=since,
            search=search or None,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: str, tracer: AgentTracer, handoffs: HandoffQueue) -> HandoffPacket:
    packet = handoffs.get(handoff_id)
    if packet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff_not_found")
    return packet


@router.get("/traces")
def search_traces(
    tracer: AgentTracer,
    search: Annotated[str | None, Query(max_length=64)] = None,
    outcome: Outcome | None = None,
    language: Language | None = None,
    offset: Annotated[int, Query(ge=0)] = 0,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> TracePage:
    """Turn traces, newest first, as summaries. ``search`` matches part of a trace,
    conversation, session or handoff ID; the full trace is read from /api/audit/{trace_id}."""
    return tracer.page(
        TraceFilter(
            search=search or None, outcome=outcome, language=language, offset=offset, limit=limit
        )
    )


@router.get("/metrics")
def agent_metrics(tracer: AgentTracer, selected: Filters) -> AuditMetrics:
    """The same metrics as /api/audit/metrics, for the console."""
    return tracer.metrics(selected)
