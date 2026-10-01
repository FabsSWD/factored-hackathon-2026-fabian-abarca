"""Audit API (M11): turn traces and metrics, for the agent role only.

The agent role is a bearer token (``AGENT_API_TOKEN``), compared in constant time and separate
from customer sessions. Any other caller, a customer with a valid session included, gets 403:
a customer can never read traces, theirs or anyone else's. Without a configured token or
tracer the API answers 503. Every read, granted or refused, is recorded as an ``audit_access``
event with what was asked (the trace or the filters) and when.

Prototype limitations: there is no identity per agent, so reads are not attributed to a person.
For the agent console (M15): the token never goes in the JavaScript bundle or the repository;
the console asks for it on a sign-in screen and keeps it only in memory.
"""

from __future__ import annotations

import hmac
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.audit.metrics import AuditMetrics
from app.audit.tracer import DatabaseAuditTracer, TraceFilter
from app.contracts import Language, Outcome, TraceRecord

router = APIRouter(prefix="/api/audit", tags=["audit"])
_bearer = HTTPBearer(auto_error=False)
AGENT_ROLE_REQUIRED = "agent_role_required"


def get_tracer(request: Request) -> DatabaseAuditTracer:
    tracer: DatabaseAuditTracer | None = getattr(request.app.state, "audit_tracer", None)
    if tracer is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audit_unavailable")
    return tracer


Tracer = Annotated[DatabaseAuditTracer, Depends(get_tracer)]


def _asked(request: Request) -> dict[str, object]:
    asked: dict[str, object] = {"query": dict(request.query_params)}
    if "trace_id" in request.path_params:
        asked["trace_id"] = request.path_params["trace_id"]
    return asked


def require_agent(
    request: Request,
    tracer: Tracer,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> DatabaseAuditTracer:
    """The tracer, once the caller proved the agent role; every attempt is recorded."""
    token: str | None = getattr(request.app.state, "agent_token", None)
    if not token:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "audit_unavailable")
    given = credentials.credentials if credentials is not None else ""
    granted = hmac.compare_digest(given.encode(), token.encode())
    tracer.record_access(request.url.path, _asked(request), granted=granted)
    if not granted:
        raise HTTPException(status.HTTP_403_FORBIDDEN, AGENT_ROLE_REQUIRED)
    return tracer


AgentTracer = Annotated[DatabaseAuditTracer, Depends(require_agent)]


def filters(
    conversation_id: str | None = None,
    session_id: str | None = None,
    outcome: Outcome | None = None,
    language: Language | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
) -> TraceFilter:
    return TraceFilter(
        conversation_id=conversation_id,
        session_id=session_id,
        outcome=outcome,
        language=language,
        since=since,
        until=until,
        limit=limit,
    )


Filters = Annotated[TraceFilter, Depends(filters)]


@router.get("/metrics")
def metrics(tracer: AgentTracer, selected: Filters) -> AuditMetrics:
    """Outcomes, containment, escalations, latency p50/p95, cost, by language and tier."""
    return tracer.metrics(selected)


@router.get("")
def list_traces(tracer: AgentTracer, selected: Filters) -> list[TraceRecord]:
    return tracer.list(selected)


@router.get("/{trace_id}")
def get_trace(trace_id: str, tracer: AgentTracer) -> TraceRecord:
    trace = tracer.get(trace_id)
    if trace is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "trace_not_found")
    return trace
