"""Test doubles for the Orchestrator: every module except the Policy Engine, the Handoff
Builder and the templates, which are the real ones.

The LLM and Kev doubles answer from a script keyed by the customer's message, can be slowed
down or made to fail, and record what they received. The Tool Layer keeps a small synthetic
bank in memory and records every read, so a test can prove that nothing was read without a
session (GATE-02).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.config import load_policy_config
from app.contracts import (
    AccessDeniedError,
    ActionId,
    CaseRecord,
    CaseStatus,
    Confirmation,
    ConversationFlags,
    CustomerRecord,
    ExtractionResult,
    HandoffPacket,
    InputGuardResult,
    LLMContext,
    ModelCall,
    ModelSignals,
    ModelSource,
    ProductRecord,
    ProvisionalCreditFlag,
    ReasonCode,
    SessionContext,
    Slots,
    Tier,
    ToolResult,
    ToolStatus,
    TraceRecord,
    TransactionRecord,
    TransactionRef,
)
from app.deadline import Deadline
from app.handoff import PolicyHandoffBuilder
from app.llm_adapter.adapter import ExtractionUnavailableError
from app.orchestrator.calls import record_call
from app.orchestrator.service import Orchestrator, OrchestratorConfig
from app.orchestrator.state import InMemoryConversationStore
from app.policy import DeterministicPolicyEngine
from app.templates.service import TemplateService

CONFIG = load_policy_config()
AS_OF = datetime(2026, 6, 18, 6, 0)
NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
CUSTOMER = "CUS-1"
OTHER = "CUS-2"
TOKEN = "token-cus-1"
OTHER_TOKEN = "token-cus-2"
KEY = "test-pseudonym-key"


# --- Identity -------------------------------------------------------------------------------------


class FakeIdentity:
    def __init__(self) -> None:
        self.sessions = {
            TOKEN: SessionContext(
                session_id="SES-1",
                customer_id=CUSTOMER,
                auth_method="test_otp",
                issued_at=NOW - timedelta(minutes=5),
                last_activity_at=NOW,
            ),
            OTHER_TOKEN: SessionContext(
                session_id="SES-2",
                customer_id=OTHER,
                auth_method="test_otp",
                issued_at=NOW - timedelta(minutes=5),
                last_activity_at=NOW,
            ),
        }
        self.expired: set[str] = set()

    def validate_session(self, token: str) -> SessionContext | None:
        if token in self.expired:
            return None
        return self.sessions.get(token)


# --- Input Guard ----------------------------------------------------------------------------------


class FakeGuard:
    """Flags messages that contain "IGNORA"; strikes per conversation, escalation at two."""

    def __init__(self) -> None:
        self.strikes: dict[str, int] = {}

    def inspect(
        self, conversation_id: str, message: str, session: SessionContext | None = None
    ) -> InputGuardResult:
        strikes = self.strikes.get(conversation_id, 0)
        if "IGNORA" not in message:
            return InputGuardResult(flagged=False, strikes=strikes)
        strikes += 1
        self.strikes[conversation_id] = strikes
        return InputGuardResult(
            flagged=True,
            strikes=strikes,
            pattern_id="override.ignore_rules.es",
            escalate_security=strikes >= CONFIG.parameters.INJECTION_STRIKES_MAX,
        )


# --- Models ---------------------------------------------------------------------------------------


def ext(
    language: str | None = "es",
    *,
    ambiguous: bool = False,
    claims: list[str] | None = None,
    flags: dict[str, bool] | None = None,
    **slots: Any,
) -> ExtractionResult:
    """An extraction as the LLM Adapter returns it."""
    return ExtractionResult(
        detected_language=language,
        language_ambiguous=ambiguous,
        slots=Slots(**slots),
        flags=ConversationFlags(**(flags or {})),
        customer_claims=claims or [],
    )


@dataclass
class ScriptedLLM:
    script: dict[str, ExtractionResult] = field(default_factory=dict)
    delay: float = 0.0
    fail: bool = False
    crash: bool = False
    contexts: list[LLMContext] = field(default_factory=list)
    connect_deadlines: list[float] = field(default_factory=list)
    extract_deadlines: list[float] = field(default_factory=list)
    calls: int = 0
    tokens_per_call: int = 500

    async def extract(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ExtractionResult:
        self.calls += 1
        self.contexts.append(context)
        if deadline is not None:
            self.extract_deadlines.append(deadline.remaining())
        if self.delay:
            await asyncio.sleep(self.delay)
        record_call(
            ModelCall(
                provider="openai",
                model="gpt-6-luna",
                purpose="extract_slots",
                input_tokens=self.tokens_per_call,
                output_tokens=40,
                latency_ms=self.delay * 1000,
                success=not (self.fail or self.crash),
            )
        )
        if self.crash:
            raise RuntimeError("unexpected adapter bug")
        if self.fail:
            from app.llm_adapter.signals import RuleBasedSignalDetector

            raise ExtractionUnavailableError(
                "HTTP 503", ExtractionResult(flags=RuleBasedSignalDetector().detect(message))
            )
        return self.script.get(message, ext(ambiguous=True))

    async def connect(
        self,
        templated_text: str,
        message: str,
        context: LLMContext,
        deadline: Deadline | None = None,
    ) -> str:
        if deadline is not None:
            self.connect_deadlines.append(deadline.remaining())
        return templated_text


@dataclass
class ScriptedKev:
    signals_by_message: dict[str, ModelSignals] = field(default_factory=dict)
    delay: float = 0.0
    calls: int = 0

    async def signals(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ModelSignals:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.signals_by_message.get(message, ModelSignals(source=ModelSource.UNAVAILABLE))


# --- Tool Layer -----------------------------------------------------------------------------------


def _txn(tid: str, **values: Any) -> TransactionRecord:
    fields: dict[str, Any] = {
        "transaction_id": tid,
        "customer_id": CUSTOMER,
        "product_id": "PRD-1",
        "transaction_type": "Purchase",
        "transaction_status": "Approved",
        "transaction_date": datetime(2026, 6, 10, 14, 30),
        "amount": Decimal("50.00"),
        "currency": "USD",
        "amount_usd": Decimal("50.00"),
        "merchant_name": "Cafe Sintetico",
        "fraud_score": 10.0,
    }
    fields.update(values)
    return TransactionRecord(**fields)


TRANSACTIONS = [
    _txn("TXN-1"),
    _txn(
        "TXN-2",
        transaction_status="Pending",
        merchant_name="Kiosko 24",
        amount=Decimal("20"),
        amount_usd=Decimal("20"),
        transaction_date=datetime(2026, 6, 17, 9),
    ),
    _txn(
        "TXN-3",
        merchant_name="Electro Mundo",
        amount=Decimal("1450.00"),
        amount_usd=Decimal("1450.00"),
        transaction_date=datetime(2026, 6, 12, 11),
    ),
    _txn(
        "TXN-4",
        merchant_name="Streaming Plus",
        amount=Decimal("18.90"),
        amount_usd=Decimal("18.90"),
        transaction_date=datetime(2026, 6, 15, 8),
    ),
    _txn(
        "TXN-5",
        merchant_name="Streaming Plus",
        amount=Decimal("18.90"),
        amount_usd=Decimal("18.90"),
        transaction_date=datetime(2026, 6, 15, 20),
    ),
]
PRODUCTS = [
    ProductRecord(
        product_id="PRD-1",
        customer_id=CUSTOMER,
        product_type="Tarjeta Crédito",
        product_number_masked="****4821",
        currency="USD",
        product_status="Active",
    ),
    ProductRecord(
        product_id="PRD-2",
        customer_id=CUSTOMER,
        product_type="Cuenta Corriente",
        product_number_masked="****1111",
        currency="USD",
        product_status="Active",
    ),
]
FOREIGN_TRANSACTION = "TXN-X"


@dataclass
class Bank:
    """The shared state behind every FakeTools view."""

    transactions: list[TransactionRecord] = field(default_factory=lambda: list(TRANSACTIONS))
    products: list[ProductRecord] = field(default_factory=lambda: list(PRODUCTS))
    cases: list[CaseRecord] = field(default_factory=list)
    packets: list[HandoffPacket] = field(default_factory=list)
    blocked: set[str] = field(default_factory=set)
    country: str = "Colombia"
    reads: list[tuple[str | None, str]] = field(default_factory=list)
    case_failure: str | None = None  # "failed" or "mismatch"
    block_failure: bool = False
    transfer_failure: bool = False
    crash_on: str | None = None


class FakeTools:
    def __init__(self, bank: Bank, session: SessionContext | None) -> None:
        self._bank = bank
        self._customer = session.customer_id if session else None

    def _read(self, what: str) -> str:
        self._bank.reads.append((self._customer, what))
        if self._bank.crash_on == what:
            raise RuntimeError(f"tool bug in {what}")
        if self._customer is None:
            raise AccessDeniedError
        return self._customer

    def get_customer(self) -> CustomerRecord:
        return CustomerRecord(customer_id=self._read("customer"), customer_status="Active")

    def customer_country(self) -> str | None:
        self._read("country")
        return self._bank.country

    def list_products(self) -> list[ProductRecord]:
        self._read("products")
        return [
            p.model_copy(update={"product_status": "Blocked"})
            if p.product_id in self._bank.blocked
            else p
            for p in self._bank.products
        ]

    def get_product(self, product_id: str) -> ProductRecord:
        self._read("product")
        found = [p for p in self.list_products() if p.product_id == product_id]
        if not found:
            raise AccessDeniedError
        return found[0]

    def transaction_candidates(self, transaction_id: str | None = None) -> list[TransactionRecord]:
        self._read("transactions")
        if transaction_id is not None and transaction_id not in {
            t.transaction_id for t in self._bank.transactions
        }:
            raise AccessDeniedError
        return list(self._bank.transactions)

    def get_transaction(self, transaction_id: str) -> TransactionRecord:
        self._read("transaction")
        for txn in self._bank.transactions:
            if txn.transaction_id == transaction_id:
                return txn
        raise AccessDeniedError

    def get_case(self, case_id: str) -> CaseRecord:
        self._read("case")
        raise AccessDeniedError

    def list_cases(self, transaction_id: str | None = None) -> list[CaseRecord]:
        self._read("cases")
        return list(self._bank.cases)

    def create_case(self, transaction_id: str, reason_code: ReasonCode, tier: Tier) -> ToolResult:
        key = f"{transaction_id}:{reason_code.value}"
        if self._bank.case_failure:
            error = "read_back_mismatch" if self._bank.case_failure == "mismatch" else "timeout"
            return ToolResult(
                action=ActionId.CREATE_CASE,
                status=ToolStatus.FAILED,
                verified=False,
                attempts=3,
                idempotency_key=key,
                error=error,
                completed_at=NOW,
            )
        txn = self.get_transaction(transaction_id)
        case_id = f"DSP-20261001-{len(self._bank.cases) + 1:06d}"
        self._bank.cases.append(
            CaseRecord(
                case_id=case_id,
                customer_id=txn.customer_id,
                transaction_id=transaction_id,
                reason_code=reason_code,
                status=CaseStatus.OPEN,
                tier=tier,
                amount=txn.amount,
                currency=txn.currency,
                amount_usd=txn.amount_usd or Decimal(0),
                provisional_credit_flag=ProvisionalCreditFlag.ELIGIBLE
                if tier is Tier.T1
                else ProvisionalCreditFlag.REQUIRES_REVIEW,
                created_at=NOW,
                business_created_at=AS_OF,
            )
        )
        return ToolResult(
            action=ActionId.CREATE_CASE,
            status=ToolStatus.SUCCESS,
            verified=True,
            attempts=1,
            idempotency_key=key,
            record_id=case_id,
            detail=f"Case {case_id} created",
            completed_at=NOW,
        )

    def block_card(self, product_id: str) -> ToolResult:
        if self._bank.block_failure:
            return ToolResult(
                action=ActionId.BLOCK_CARD,
                status=ToolStatus.FAILED,
                verified=False,
                attempts=3,
                error="timeout",
                completed_at=NOW,
            )
        self._bank.blocked.add(product_id)
        return ToolResult(
            action=ActionId.BLOCK_CARD,
            status=ToolStatus.SUCCESS,
            verified=True,
            attempts=1,
            record_id=product_id,
            detail="Card ending 4821 blocked",
            completed_at=NOW,
        )

    def transfer_to_human(self, packet: HandoffPacket) -> ToolResult:
        if self._bank.transfer_failure:
            return ToolResult(
                action=ActionId.TRANSFER_TO_HUMAN,
                status=ToolStatus.FAILED,
                verified=False,
                attempts=3,
                error="queue down",
                completed_at=NOW,
            )
        self._bank.packets.append(packet)
        return ToolResult(
            action=ActionId.TRANSFER_TO_HUMAN,
            status=ToolStatus.SUCCESS,
            verified=True,
            attempts=1,
            record_id=packet.handoff_id,
            completed_at=NOW,
        )


class MemoryTracer:
    def __init__(self) -> None:
        self.traces: list[TraceRecord] = []

    def record(self, trace: TraceRecord) -> None:
        if any(t.trace_id == trace.trace_id for t in self.traces):
            raise AssertionError("one trace per turn")
        self.traces.append(trace)

    def get(self, trace_id: str) -> TraceRecord | None:
        return next((t for t in self.traces if t.trace_id == trace_id), None)


# --- Assembly -------------------------------------------------------------------------------------


@dataclass
class World:
    identity: FakeIdentity
    guard: FakeGuard
    llm: ScriptedLLM
    kev: ScriptedKev
    bank: Bank
    tracer: MemoryTracer
    orchestrator: Orchestrator
    engine: DeterministicPolicyEngine

    def say(
        self,
        message: str,
        extraction: ExtractionResult | None = None,
        signals: ModelSignals | None = None,
    ) -> None:
        if extraction is not None:
            self.llm.script[message] = extraction
        if signals is not None:
            self.kev.signals_by_message[message] = signals

    def turn(self, message: str, conversation_id: str = "CONV-1", token: str | None = TOKEN) -> Any:
        return asyncio.run(self.orchestrator.handle_turn(conversation_id, message, token))


def build_world(
    *,
    token_cap: int | None = None,
    turn_deadline: float = 20.0,
    engine_factory: Callable[[], DeterministicPolicyEngine] | None = None,
) -> World:
    identity, guard, llm, kev = FakeIdentity(), FakeGuard(), ScriptedLLM(), ScriptedKev()
    bank, tracer = Bank(), MemoryTracer()
    engine = engine_factory() if engine_factory else DeterministicPolicyEngine(CONFIG)
    numbers = iter(range(1, 10_000))
    builder = PolicyHandoffBuilder(
        CONFIG.parameters, KEY, lambda at: f"HO-{at:%Y%m%d}-{next(numbers):06d}", clock=lambda: NOW
    )
    orchestrator = Orchestrator(
        identity=identity,
        guard=guard,
        llm=llm,
        decision=kev,
        engine=engine,
        tools=lambda session, cid: FakeTools(bank, session),
        builder=builder,
        templates=TemplateService.from_policy(CONFIG.parameters),
        tracer=tracer,
        store=InMemoryConversationStore(),
        config=OrchestratorConfig(
            as_of=AS_OF,
            parameters=CONFIG.parameters,
            policy_version=CONFIG.policy_version,
            pseudonym_key=KEY,
            turn_deadline_seconds=turn_deadline,
            token_cap=token_cap,
        ),
        clock=lambda: NOW,
    )
    return World(identity, guard, llm, kev, bank, tracer, orchestrator, engine)


def timer() -> Callable[[], float]:
    started = time.perf_counter()
    return lambda: time.perf_counter() - started


REF_CAFE = TransactionRef(
    transaction_date=date(2026, 6, 10), amount=Decimal("50"), merchant="Cafe Sintetico"
)
YES = Confirmation.CONFIRMED
KEV_FALLBACK = ModelSignals(source=ModelSource.LLM_FALLBACK)
