"""Module interfaces (architecture §3).

Each backend module implements one of these protocols, and the Orchestrator depends only on
the protocols. Modules built in parallel can therefore be tested against fakes of each other.

Calls to external models are ``async`` so the Orchestrator can run them in parallel
(architecture §4, step 4). Everything else is synchronous; the Orchestrator runs blocking
database work in a thread pool.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol, runtime_checkable

from app.contracts import (
    CaseRecord,
    CustomerRecord,
    ExtractionResult,
    HandoffPacket,
    InputGuardResult,
    Language,
    LLMContext,
    ModelSignals,
    PolicyDecision,
    PolicyRequest,
    ProductRecord,
    ReasonCode,
    SessionContext,
    SlotName,
    Tier,
    ToolResult,
    TraceRecord,
    TransactionRecord,
    TransactionRef,
)
from app.deadline import Deadline


@runtime_checkable
class IdentityService(Protocol):
    """M2. Issues and validates sessions (GATE-02)."""

    def validate_session(self, token: str) -> SessionContext | None:
        """Return the session for a valid token, or ``None`` if it is missing, tampered with,
        too old, or idle for too long. Refreshes ``last_activity_at`` on success."""
        ...


@runtime_checkable
class InputGuard(Protocol):
    """M4. Detects manipulation attempts before text reaches the LLM (ESC-13).

    Strikes are counted per conversation, from its first message and before authentication;
    re-authenticating or an expired session never resets them. A new conversation starts at
    zero (a known limitation). When a session exists, the security event is linked to it and
    to its customer, for monitoring across conversations (never a rule).

    Contract for the Orchestrator (M12) when ``flagged`` is true:

    - The message reaches neither the LLM Adapter nor the Decision Client, not even its
      legitimate part.
    - The reply is the neutral ``ask_rephrase`` template: it asks the customer to rephrase and
      never reveals what was detected. With ``escalate_security``, the turn escalates under
      ESC-13 instead.
    - A flagged attempt never counts as a clarification turn.
    """

    def inspect(
        self, conversation_id: str, message: str, session: SessionContext | None = None
    ) -> InputGuardResult: ...


@runtime_checkable
class LLMAdapter(Protocol):
    """M5. The only path to the language model. Accepts only DATA-01 data.

    Both calls share one turn deadline (``LLM_TURN_DEADLINE_SECONDS``): create it once per turn
    and pass it to ``extract`` and then ``connect``; ``connect`` uses what is left.

    Contract for the Orchestrator (M12) when ``extract`` raises ``LLMError``: the turn goes on
    with empty slots and only the rule-based signals (``human_requested``,
    ``legal_or_vulnerability``), available as ``error.fallback`` on
    ``ExtractionUnavailableError``. The customer always gets a reply; an error is never
    left unanswered.
    """

    async def extract(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ExtractionResult: ...

    async def connect(
        self,
        templated_text: str,
        message: str,
        context: LLMContext,
        deadline: Deadline | None = None,
        *,
        brief: bool = False,
        previous: Sequence[str] = (),
    ) -> str:
        """Write connecting sentences around committed template text (COM-02).

        The returned text must contain ``templated_text`` unchanged."""
        ...


@runtime_checkable
class DecisionClient(Protocol):
    """M6. Kev's signals for the current message; never raises.

    Returns ``source=kev`` signals, or ``source=unavailable`` when Kev is not configured, fails,
    times out or answers outside the contract. Contract for the Orchestrator (M12): run it in
    parallel with ``LLMAdapter.extract`` on the same turn deadline, then call
    ``app.decision.resolve_signals(kev_signals, extraction)``, which falls back to signals
    derived from the current message's extraction (``llm_fallback``) and otherwise keeps
    ``unavailable``. When ``extract`` failed, pass ``ExtractionUnavailableError.fallback``.
    """

    async def signals(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ModelSignals: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """M8. The only component that decides outcomes. Pure: no I/O, no clock, no models."""

    def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...

    def explain(self, decision: PolicyDecision) -> list[str]:
        """Explanation built from rule identifiers and records (policy principle 5)."""
        ...


@runtime_checkable
class ToolLayer(Protocol):
    """M9. Reads and actions, bound at construction to one session's customer, or to none
    before authentication (then only ACT-05 works: GATE-02 forbids reading account data).

    No method takes a customer ID: permissions always use the session's (GATE-04).
    Reads raise ``AccessDeniedError`` for records the customer cannot access, whether they
    exist or not; actions return ``access_denied``. Either way a security event is recorded.
    The Tool Layer only reads and writes: matching and counting rules live in the Policy
    Engine. Prohibited actions (ACT-06) have no method.
    """

    # ACT-01 reads
    def get_customer(self) -> CustomerRecord: ...

    def list_products(self) -> list[ProductRecord]: ...

    def customer_country(self) -> str | None:
        """Presentation only (amount format, COM-08): never part of a PolicyRequest (DATA-02)."""
        ...

    def get_product(self, product_id: str) -> ProductRecord: ...

    def transaction_candidates(self, transaction_id: str | None = None) -> list[TransactionRecord]:
        """``PolicyRequest.transaction_candidates``: the customer's transactions within
        ``LATE_WINDOW_DAYS`` of ``as_of``, plus ``transaction_id`` if the customer gave one."""
        ...

    def get_transaction(self, transaction_id: str) -> TransactionRecord: ...

    def get_case(self, case_id: str) -> CaseRecord:
        """Filtered by the session customer: case numbers are sequential."""
        ...

    def list_cases(self, transaction_id: str | None = None) -> list[CaseRecord]: ...

    # Actions
    def create_case(self, transaction_id: str, reason_code: ReasonCode, tier: Tier) -> ToolResult:
        """ACT-02 with ACT-04. Idempotency key: transaction_id + reason_code."""
        ...

    def block_card(self, product_id: str) -> ToolResult:
        """ACT-03. Verified by reading back ``product_status = 'Blocked'``."""
        ...

    def transfer_to_human(self, packet: HandoffPacket) -> ToolResult:
        """ACT-05. Verified by the queue acknowledgement."""
        ...

    def append_handoff_message(self, handoff_id: str, text: str) -> None:
        """Add a message the customer wrote after the handoff (already masked) to its packet,
        for the agent. Only the session's own handoff: ``AccessDeniedError`` otherwise."""
        ...


@runtime_checkable
class HandoffBuilder(Protocol):
    """M10. Builds the §13 packet for ESCALATE outcomes only. The summary, the escalation
    reasons and the verified facts are built from the decision and the records; pass the
    results of the actions already run in the turn, so facts reflect them."""

    def build(
        self,
        *,
        request: PolicyRequest,
        decision: PolicyDecision,
        language: Language,
        customer_claims: Sequence[str],
        actions_taken: Sequence[ToolResult],
        open_questions: Sequence[str],
        transcript_ref: str,
        evidence_claims: Mapping[str, Sequence[str]] | None = None,
        slot_turns: Mapping[SlotName, int] | None = None,
        transaction_ref_said: TransactionRef | None = None,
        picked_candidate: int | None = None,
    ) -> HandoffPacket: ...


@runtime_checkable
class TemplateService(Protocol):
    """M3. Versioned customer commitments (COM-02)."""

    def render(self, template_id: str, language: Language, **values: object) -> str: ...


@runtime_checkable
class AuditTracer(Protocol):
    """M11. One trace record per turn (architecture §9)."""

    def record(self, trace: TraceRecord) -> None: ...

    def get(self, trace_id: str) -> TraceRecord | None: ...
