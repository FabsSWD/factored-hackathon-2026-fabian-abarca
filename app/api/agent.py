"""Agent console API (M12, for M15): the queue of escalated cases, each handoff packet, and the
operating metrics. Agent role only, with the same token and access log as the audit API."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from app.api.audit import AgentTracer, Filters
from app.audit.metrics import AuditMetrics
from app.contracts import HandoffPacket, Priority, Queue
from app.handoff.queue import DatabaseHandoffQueue, HandoffFilter, HandoffSummary

router = APIRouter(prefix="/api/agent", tags=["agent"])


def get_queue(request: Request) -> DatabaseHandoffQueue:
    queue: DatabaseHandoffQueue | None = getattr(request.app.state, "handoff_queue", None)
    if queue is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "agent_console_unavailable")
    return queue


HandoffQueue = Annotated[DatabaseHandoffQueue, Depends(get_queue)]


@router.get("/handoffs")
def list_handoffs(
    tracer: AgentTracer,
    handoffs: HandoffQueue,
    queue: Queue | None = None,
    priority: Priority | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 50,
) -> list[HandoffSummary]:
    """Escalated cases, high priority first, then the oldest."""
    return handoffs.list(HandoffFilter(queue=queue, priority=priority, since=since, limit=limit))


@router.get("/handoffs/{handoff_id}")
def get_handoff(handoff_id: str, tracer: AgentTracer, handoffs: HandoffQueue) -> HandoffPacket:
    packet = handoffs.get(handoff_id)
    if packet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "handoff_not_found")
    return packet


@router.get("/metrics")
def agent_metrics(tracer: AgentTracer, selected: Filters) -> AuditMetrics:
    """The same metrics as /api/audit/metrics, for the console."""
    return tracer.metrics(selected)
