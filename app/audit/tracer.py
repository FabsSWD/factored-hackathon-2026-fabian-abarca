"""M11 Audit Tracer: one trace record per turn, stored in ``audit_logs`` (architecture §9).

The trace is the ``TraceRecord`` contract serialized whole: optional stages that did not run
are stored as ``null``, never omitted. The customer's message is kept according to
``AUDIT_MESSAGE_MODE`` (masked by default) before it is written, so no caller can bypass it.
The contract has no field for secrets or DATA-01/DATA-02 data, so they cannot be traced.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Select, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.audit.masking import AuditMessageMode, retain_message
from app.audit.metrics import AuditMetrics, compute_metrics
from app.contracts import Language, Outcome, TraceRecord
from app.storage.models import AuditLog

TRACE_EVENT = "turn_trace"
ACCESS_EVENT = "audit_access"
METRICS_MAX_TRACES = 100_000


class DuplicateTraceError(RuntimeError):
    """A trace with this ``trace_id`` exists: each turn produces exactly one trace."""


class TraceFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    conversation_id: str | None = None
    session_id: str | None = None
    outcome: Outcome | None = None
    language: Language | None = None
    since: datetime | None = None  # inclusive, on created_at
    until: datetime | None = None  # exclusive
    limit: int = Field(default=100, ge=1, le=1000)


class DatabaseAuditTracer:
    """Implements ``app.interfaces.AuditTracer``, plus listing and metrics for the audit API."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        message_mode: AuditMessageMode = AuditMessageMode.MASKED,
    ) -> None:
        self._sessions = session_factory
        self._mode = message_mode

    def record(self, trace: TraceRecord) -> None:
        kept = trace.model_copy(update={"message": retain_message(trace.message, self._mode)})
        with self._sessions() as db:
            db.add(
                AuditLog(
                    event_type=TRACE_EVENT,
                    trace_id=kept.trace_id,
                    conversation_id=kept.conversation_id,
                    session_id=kept.session_id,
                    payload=kept.model_dump(mode="json"),
                    created_at=kept.created_at,
                )
            )
            try:
                db.commit()
            except IntegrityError as exc:
                db.rollback()
                if "trace_id" in str(exc.orig):
                    raise DuplicateTraceError(kept.trace_id) from exc
                raise

    def record_access(self, endpoint: str, details: dict[str, object], *, granted: bool) -> None:
        """Every read of the audit API: what was read (trace or filters), when, and whether
        access was granted. There is no per-agent identity, so reads are not attributed to a
        person (a documented limitation)."""
        with self._sessions() as db:
            db.add(
                AuditLog(
                    event_type=ACCESS_EVENT,
                    payload={"endpoint": endpoint, "granted": granted, **details},
                )
            )
            db.commit()

    def get(self, trace_id: str) -> TraceRecord | None:
        with self._sessions() as db:
            payload = db.scalar(
                select(AuditLog.payload).where(
                    AuditLog.event_type == TRACE_EVENT, AuditLog.trace_id == trace_id
                )
            )
        return TraceRecord.model_validate(payload) if payload is not None else None

    def list(self, filters: TraceFilter) -> list[TraceRecord]:
        """Matching traces, newest first."""
        query = self._query(filters).order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
        with self._sessions() as db:
            payloads = db.scalars(query.limit(filters.limit)).all()
        return [TraceRecord.model_validate(payload) for payload in payloads]

    def metrics(self, filters: TraceFilter) -> AuditMetrics:
        with self._sessions() as db:
            payloads = db.scalars(self._query(filters).limit(METRICS_MAX_TRACES)).all()
        return compute_metrics([TraceRecord.model_validate(payload) for payload in payloads])

    @staticmethod
    def _query(filters: TraceFilter) -> Select[tuple[dict[str, object]]]:
        query = select(AuditLog.payload).where(AuditLog.event_type == TRACE_EVENT)
        if filters.conversation_id is not None:
            query = query.where(AuditLog.conversation_id == filters.conversation_id)
        if filters.session_id is not None:
            query = query.where(AuditLog.session_id == filters.session_id)
        if filters.outcome is not None:
            query = query.where(AuditLog.payload["outcome"].astext == filters.outcome.value)
        if filters.language is not None:
            query = query.where(AuditLog.payload["language"].astext == filters.language.value)
        if filters.since is not None:
            query = query.where(AuditLog.created_at >= filters.since)
        if filters.until is not None:
            query = query.where(AuditLog.created_at < filters.until)
        return query
