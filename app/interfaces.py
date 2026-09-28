"""Module interfaces (architecture §3).

Each backend module implements one of these protocols, and the Orchestrator depends only on
the protocols. Modules built in parallel can therefore be tested against fakes of each other.

Calls to external models are ``async`` so the Orchestrator can run them in parallel
(architecture §4, step 4). Everything else is synchronous; the Orchestrator runs blocking
database work in a thread pool.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from app.contracts import (
    CaseRecord,
    CustomerRecord,
    DisputeHistory,
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
    Tier,
    ToolResult,
    TraceRecord,
    TransactionRecord,
    TransactionRef,
)


@runtime_checkable
class IdentityService(Protocol):
    """M2. Issues and validates sessions (GATE-02)."""

    def validate_session(self, token: str) -> SessionContext | None:
        """Return the session for a valid token, or ``None`` if it is missing, tampered with,
        too old, or idle for too long. Refreshes ``last_activity_at`` on success."""
        ...


@runtime_checkable
class InputGuard(Protocol):
    """M4. Detects manipulation attempts before text reaches the LLM (ESC-13)."""

    def inspect(self, session_id: str, message: str) -> InputGuardResult: ...


@runtime_checkable
class LLMAdapter(Protocol):
    """M5. The only path to the language model. Accepts only DATA-01 data."""

    async def extract(self, message: str, context: LLMContext) -> ExtractionResult: ...

    async def connect(self, templated_text: str, message: str, context: LLMContext) -> str:
        """Write connecting sentences around committed template text (COM-02).

        The returned text must contain ``templated_text`` unchanged."""
        ...


@runtime_checkable
class DecisionClient(Protocol):
    """M6. Returns decision-layer signals; never raises, returns UNAVAILABLE signals instead."""

    async def signals(self, message: str, context: LLMContext) -> ModelSignals: ...


@runtime_checkable
class PolicyEngine(Protocol):
    """M8. The only component that decides outcomes. Pure: no I/O, no clock, no models."""

    def evaluate(self, request: PolicyRequest) -> PolicyDecision: ...

    def explain(self, decision: PolicyDecision) -> list[str]:
        """Explanation built from rule identifiers and records (policy principle 5)."""
        ...


@runtime_checkable
class ToolLayer(Protocol):
    """M9. Reads and actions, bound to one session's customer at construction.

    No method takes a customer ID: permissions always use the session's (GATE-04).
    Reads raise ``AccessDeniedError`` for records the customer cannot access, whether they
    exist or not. Prohibited actions (ACT-06) have no method.
    """

    # ACT-01 reads
    def get_customer(self) -> CustomerRecord: ...

    def list_products(self) -> list[ProductRecord]: ...

    def get_product(self, product_id: str) -> ProductRecord: ...

    def find_transactions(self, ref: TransactionRef) -> list[TransactionRecord]: ...

    def get_transaction(self, transaction_id: str) -> TransactionRecord: ...

    def find_duplicate_candidates(self, transaction_id: str) -> list[TransactionRecord]: ...

    def list_cases(self, transaction_id: str | None = None) -> list[CaseRecord]: ...

    def get_dispute_history(self) -> DisputeHistory: ...

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


@runtime_checkable
class HandoffBuilder(Protocol):
    """M10. Builds the §13 packet for ESCALATE outcomes only."""

    def build(
        self,
        *,
        request: PolicyRequest,
        decision: PolicyDecision,
        language: Language,
        request_summary: str,
        customer_claims: Sequence[str],
        actions_taken: Sequence[ToolResult],
        open_questions: Sequence[str],
        transcript_ref: str,
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
