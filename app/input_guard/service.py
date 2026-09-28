"""Input Guard (architecture §3): manipulation attempts before text reaches the LLM (ESC-13).

For each message: rules detect instruction overrides, staff impersonation, requests for other
customers' data, and prompt extraction (``app.input_guard.patterns``). A flagged message is a
strike for its conversation:

- strike 1 is ignored (the attempt is not obeyed) and recorded;
- at INJECTION_STRIKES_MAX strikes, ``escalate_security`` asks to end automation
  (ESC-13, security review queue). Later strikes keep that signal.

Strikes are keyed by ``conversation_id``, which exists from the first message, before
authentication, so re-authenticating or an expired session never resets them. Every strike is
a security event in ``audit_logs`` (``conversation_id`` column); the count comes from those
events, so it survives restarts and is never shared between conversations. When a session
exists, the event is linked to it and carries its ``customer_id``, so M11 can show attempts
per customer across conversations (monitoring only, never a rule). Limitation: a new
conversation starts again at zero. Events carry the pattern and the strike number, never the
message text.

What the Orchestrator does with a flagged message is part of the InputGuard contract in
``app.interfaces``.

Optional hook: a manipulation classifier (for example a Kev question, M6/M7) can flag messages
the rules miss. It never un-flags a rule match, and without it the guard is rules only.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import InputGuardResult, SessionContext
from app.input_guard.patterns import Category, detect
from app.storage.models import AuditLog

EVENT_KIND = "injection_attempt"
CLASSIFIER_PATTERN_ID = "model.manipulation"

ManipulationClassifier = Callable[[str], float | None]
"""Returns the probability that a message is a manipulation attempt, or None if unavailable."""


class RuleBasedInputGuard:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        strikes_max: int,
        classifier: ManipulationClassifier | None = None,
        classifier_threshold: float | None = None,
    ) -> None:
        if strikes_max < 1:
            raise ValueError("strikes_max must be at least 1")
        if (classifier is None) != (classifier_threshold is None):
            raise ValueError("a classifier needs a threshold, and a threshold needs a classifier")
        if classifier_threshold is not None and not 0.0 < classifier_threshold <= 1.0:
            raise ValueError("classifier_threshold must be in (0, 1]")
        self._sessions = session_factory
        self._strikes_max = strikes_max
        self._classifier = classifier
        self._threshold = classifier_threshold

    def inspect(
        self, conversation_id: str, message: str, session: SessionContext | None = None
    ) -> InputGuardResult:
        if not conversation_id:
            raise ValueError("conversation_id is required: strikes are counted per conversation")
        pattern_id, category = self._classify(message)
        with self._sessions() as db:
            strikes = self._strikes(db, conversation_id)
            if pattern_id is None:
                return InputGuardResult(flagged=False, strikes=strikes)
            strikes += 1
            escalate = strikes >= self._strikes_max
            db.add(
                AuditLog(
                    event_type="security_event",
                    conversation_id=conversation_id,
                    session_id=session.session_id if session else None,
                    payload={
                        "kind": EVENT_KIND,
                        "customer_id": session.customer_id if session else None,
                        "pattern_id": pattern_id,
                        "category": category,
                        "strike": strikes,
                        "escalate_security": escalate,
                    },
                )
            )
            db.commit()
        return InputGuardResult(
            flagged=True, strikes=strikes, pattern_id=pattern_id, escalate_security=escalate
        )

    def _classify(self, message: str) -> tuple[str | None, str | None]:
        pattern = detect(message)
        if pattern is not None:
            return pattern.pattern_id, pattern.category.value
        if self._classifier is not None and self._threshold is not None:
            probability = self._classifier(message)
            if probability is not None and probability >= self._threshold:
                return CLASSIFIER_PATTERN_ID, Category.INSTRUCTION_OVERRIDE.value
        return None, None

    @staticmethod
    def _strikes(db: Session, conversation_id: str) -> int:
        count = db.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.event_type == "security_event",
                AuditLog.conversation_id == conversation_id,
                AuditLog.payload["kind"].astext == EVENT_KIND,
            )
        )
        return int(count or 0)
