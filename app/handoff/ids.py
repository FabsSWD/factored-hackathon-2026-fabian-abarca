"""Handoff IDs ``HO-YYYYMMDD-NNNNNN`` (policy §13), numbered by ``handoff_number_seq``."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.storage.models import HANDOFF_NUMBER_SEQ


class SequenceHandoffIds:
    """``HO-`` + the creation date (real time, UTC) + the next sequence value, zero-padded to
    six digits and never wrapped (the CHECK accepts six digits or more)."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._sessions = session_factory

    def __call__(self, created_at: datetime) -> str:
        with self._sessions() as db:
            number = db.scalar(select(HANDOFF_NUMBER_SEQ.next_value()))
        assert number is not None
        return f"HO-{created_at:%Y%m%d}-{number:06d}"
