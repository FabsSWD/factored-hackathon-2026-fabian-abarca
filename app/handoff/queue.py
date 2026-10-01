"""The queue of escalated cases the agent console reads (M12, M15): ``handoff_packets``.

Read-only: packets are written by ACT-05 (``ToolLayer.transfer_to_human``). High priority
first, then the oldest, so the agent takes the most urgent and longest-waiting case.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from sqlalchemy import case, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import HandoffPacket, Language, Priority, Queue
from app.storage.models import HandoffPacketRow


class HandoffSummary(BaseModel):
    """One line of the queue: enough to choose a case without opening it."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    handoff_id: str
    created_at: AwareDatetime
    queue: Queue
    priority: Priority
    status: str
    language: Language
    request_summary: str
    triggered_rules: list[str]


class HandoffFilter(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    queue: Queue | None = None
    priority: Priority | None = None
    since: datetime | None = None
    limit: int = Field(default=50, ge=1, le=500)


class DatabaseHandoffQueue:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def list(self, filters: HandoffFilter) -> list[HandoffSummary]:
        urgent_first = case((HandoffPacketRow.priority == Priority.HIGH.value, 0), else_=1)
        query = select(HandoffPacketRow).order_by(
            urgent_first, HandoffPacketRow.created_at, HandoffPacketRow.handoff_id
        )
        if filters.queue is not None:
            query = query.where(HandoffPacketRow.queue == filters.queue.value)
        if filters.priority is not None:
            query = query.where(HandoffPacketRow.priority == filters.priority.value)
        if filters.since is not None:
            query = query.where(HandoffPacketRow.created_at >= filters.since)
        with self._sessions() as db:
            rows = db.scalars(query.limit(filters.limit)).all()
        summaries = []
        for row in rows:
            packet = HandoffPacket.model_validate(row.packet)
            summaries.append(
                HandoffSummary(
                    handoff_id=packet.handoff_id,
                    created_at=packet.created_at,
                    queue=packet.queue,
                    priority=packet.priority,
                    status=row.status,
                    language=packet.language,
                    request_summary=packet.request_summary,
                    triggered_rules=list(packet.triggered_rules),
                )
            )
        return summaries

    def get(self, handoff_id: str) -> HandoffPacket | None:
        with self._sessions() as db:
            row = db.get(HandoffPacketRow, handoff_id)
        return HandoffPacket.model_validate(row.packet) if row is not None else None
