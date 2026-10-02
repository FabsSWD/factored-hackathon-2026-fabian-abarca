"""M13 end-to-end harness: the real application through its HTTP API.

Real modules: the FastAPI app, the Identity Service (document + OTP login), the Input Guard, the
Policy Engine, the Tool Layer, the Handoff Builder with database-numbered handoffs, the audit
tracer and the handoff queue, all on the temporary PostgreSQL database with the synthetic Core
Banking fixtures (tests/fixtures/core_banking.py). Doubles: only the two model services, OpenAI
(the LLM Adapter answers from a script keyed by the customer's message) and Kev.

Each test runs inside a transaction that is rolled back (tests/conftest.py).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Connection, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import RateLimits
from app.audit.tracer import DatabaseAuditTracer
from app.config import load_policy_config
from app.contracts import (
    ExtractionResult,
    HandoffPacket,
    ModelSignals,
    SessionContext,
    TraceRecord,
)
from app.handoff import PolicyHandoffBuilder, SequenceHandoffIds
from app.handoff.queue import DatabaseHandoffQueue
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityService
from app.input_guard.service import RuleBasedInputGuard
from app.interfaces import ToolLayer
from app.main import create_app
from app.orchestrator.service import Orchestrator, OrchestratorConfig
from app.orchestrator.state import InMemoryConversationStore
from app.policy import DeterministicPolicyEngine
from app.settings import Settings
from app.storage.models import AuditLog, Case, HandoffPacketRow
from app.templates.service import TemplateService
from app.tools import DatabaseToolLayer, ToolConfig
from tests.conftest import Pipeline
from tests.identity.conftest import OTP, FakeClock, identity_config
from tests.orchestrator.fakes import ScriptedKev, ScriptedLLM

CONFIG = load_policy_config()
AS_OF = datetime(2026, 6, 18, 6, 0)  # business date 2026-06-17, cutoff 06:00
BUSINESS_DATE = date(2026, 6, 17)
START = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
PSEUDONYM_KEY = "e2e-pseudonym-key"
AGENT_TOKEN = "agent-token-for-e2e-0123456789abcdef"
AGENT = {"Authorization": f"Bearer {AGENT_TOKEN}"}

DOCUMENT = "X1234567"  # CLI-ALPHA0000001: card ****4821, account, loan, ARS account
OTHER_DOCUMENT = "42388496"  # CLI-BETA00000002
SUSPENDED_DOCUMENT = "C9988776"  # CLI-GAMMA0000003, customer_status Suspended

ToolWrapper = Callable[[DatabaseToolLayer], ToolLayer]

# Checked on every reply of every end-to-end turn (security review, COM-07, COM-08): no rule
# identifier, threshold or internal identifier, no full card or document number, no trace of an
# internal error, no amount formatted without its currency code, no number of days but the
# resolution commitment.
FORBIDDEN_IN_REPLIES = re.compile(
    r"\b(?:GATE|ESC|ACT|COM|DATA)-\d{2}\b|\bRC_[A-Z_]+|\bT[123]\b|\bTRX-|\bCLI-|\bPRD-"
    r"|\bHO-\d|4111111111114821|X1234567|42388496|C9988776|fraud|score|threshold|umbral"
    r"|Traceback|Exception|Error\b|None\b|\{|\}"
)
UNCODED_AMOUNT = re.compile(r"(?<![A-Z]{3} )(?<![\d.,])\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?!\d)")
DAYS = re.compile(r"\b(\d+)\s+d[ií]as\b")


def assert_customer_safe(reply: str) -> None:
    assert not FORBIDDEN_IN_REPLIES.search(reply), reply
    assert not UNCODED_AMOUNT.search(reply), reply
    assert all(
        int(days) == CONFIG.parameters.RESOLUTION_TARGET_BUSINESS_DAYS
        for days in DAYS.findall(reply)
    ), reply


@dataclass
class Turn:
    """One reply as the customer sees it, and the audit trace behind it."""

    status: int
    body: dict[str, Any]
    trace: TraceRecord | None

    @property
    def reply(self) -> str:
        return str(self.body.get("reply", ""))

    @property
    def outcome(self) -> str | None:
        return self.trace.outcome.value if self.trace and self.trace.outcome else None

    @property
    def rules(self) -> list[str]:
        if self.trace is None:
            return []
        return sorted({rule for d in self.trace.decisions for rule in d.triggered_rules})

    @property
    def handed_off(self) -> bool:
        return bool(self.body.get("handed_off"))


@dataclass
class Conversation:
    e2e: E2E
    token: str | None
    conversation_id: str | None = None
    turns: list[Turn] = field(default_factory=list)

    def send(
        self,
        message: str,
        extraction: ExtractionResult | None = None,
        signals: ModelSignals | None = None,
        token: str | bool | None = True,
    ) -> Turn:
        """``extraction``: what the OpenAI double returns for this message; ``signals``: Kev's."""
        if extraction is not None:
            self.e2e.llm.script[message] = extraction
        if signals is not None:
            self.e2e.kev.signals_by_message[message] = signals
        bearer = self.token if token is True else token
        body: dict[str, Any] = {"message": message}
        if self.conversation_id:
            body["conversation_id"] = self.conversation_id
        headers = {"Authorization": f"Bearer {bearer}"} if bearer else {}
        response = self.e2e.client.post("/api/turn", json=body, headers=headers)
        data = response.json()
        if response.status_code == 200:
            self.conversation_id = data["conversation_id"]
        trace = self.e2e.tracer.get(data["trace_id"]) if "trace_id" in data else None
        turn = Turn(response.status_code, data, trace)
        assert_customer_safe(turn.reply)
        self.turns.append(turn)
        return turn


@dataclass
class E2E:
    client: TestClient
    llm: ScriptedLLM
    kev: ScriptedKev
    db: Session
    tracer: DatabaseAuditTracer
    queue: DatabaseHandoffQueue
    clock: FakeClock
    tool_wrapper: list[ToolWrapper]

    def login(self, document: str = DOCUMENT) -> str:
        login = self.client.post("/auth/login", json={"document_number": document})
        assert login.status_code == 202, login.text
        verify = self.client.post("/auth/verify", json={"document_number": document, "otp": OTP})
        assert verify.status_code == 200, verify.text
        token = verify.json()["access_token"]
        assert isinstance(token, str)
        return token

    def conversation(self, document: str | None = DOCUMENT) -> Conversation:
        return Conversation(self, self.login(document) if document else None)

    def packet(self, handoff_id: str | None) -> HandoffPacket:
        assert handoff_id is not None
        packet = self.queue.get(handoff_id)
        assert packet is not None
        return packet

    def handoff_of(self, turn: Turn) -> HandoffPacket:
        assert turn.trace is not None
        return self.packet(turn.trace.handoff_id)

    def cases(self) -> list[Case]:
        self.db.expire_all()
        return list(self.db.scalars(select(Case).order_by(Case.case_id)))

    def handoffs(self) -> list[HandoffPacketRow]:
        self.db.expire_all()
        return list(self.db.scalars(select(HandoffPacketRow).order_by(HandoffPacketRow.handoff_id)))

    def security_events(self) -> list[dict[str, Any]]:
        self.db.expire_all()
        rows = self.db.scalars(
            select(AuditLog).where(AuditLog.event_type == "security_event").order_by(AuditLog.id)
        )
        return [row.payload for row in rows]


def e2e_settings() -> Settings:
    return Settings(
        _env_file=None,
        business_date=BUSINESS_DATE,
        pseudonym_key=SecretStr(PSEUDONYM_KEY),
        agent_api_token=SecretStr(AGENT_TOKEN),
    )


@pytest.fixture
def e2e_sessions(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def e2e(e2e_sessions: sessionmaker[Session], db_session: Session) -> Iterator[E2E]:
    parameters = CONFIG.parameters
    clock = FakeClock(START)
    llm, kev = ScriptedLLM(tokens_per_call=0), ScriptedKev()
    tracer = DatabaseAuditTracer(e2e_sessions)
    queue = DatabaseHandoffQueue(e2e_sessions)
    tool_config = ToolConfig(as_of=AS_OF, parameters=parameters, retry_wait_seconds=0)
    wrapper: list[ToolWrapper] = []

    def tools(session: SessionContext | None, conversation_id: str) -> ToolLayer:
        layer = DatabaseToolLayer(
            e2e_sessions, session, tool_config, conversation_id=conversation_id, clock=clock
        )
        return wrapper[0](layer) if wrapper else layer

    identity = IdentityService(identity_config(), e2e_sessions, clock)
    orchestrator = Orchestrator(
        identity=identity,
        guard=RuleBasedInputGuard(e2e_sessions, parameters.INJECTION_STRIKES_MAX),
        llm=llm,
        decision=kev,
        engine=DeterministicPolicyEngine(CONFIG),
        tools=tools,
        builder=PolicyHandoffBuilder(
            parameters, PSEUDONYM_KEY, SequenceHandoffIds(e2e_sessions), clock=clock
        ),
        templates=TemplateService.from_policy(parameters),
        tracer=tracer,
        store=InMemoryConversationStore(),
        config=OrchestratorConfig(
            as_of=AS_OF,
            parameters=parameters,
            policy_version=CONFIG.policy_version,
            pseudonym_key=PSEUDONYM_KEY,
            turn_deadline_seconds=20.0,
        ),
        clock=clock,
    )
    app = create_app(
        CONFIG,
        identity=identity,
        rate_limits=RateLimits(per_session=RateLimiter(10_000), auth_per_ip=RateLimiter(10_000)),
        settings=e2e_settings(),
        audit_tracer=tracer,
        orchestrator=orchestrator,
        handoff_queue=queue,
    )
    with TestClient(app) as client:
        yield E2E(client, llm, kev, db_session, tracer, queue, clock, wrapper)
