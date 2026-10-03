"""M18 harness end to end, without any real model: the real application (Identity Service,
Input Guard, Policy Engine, Tool Layer, handoffs, audit) on the test database with the seeded
cases, wrapped by the harness (faults per conversation, signal capture), played by the customer
simulator over the API. OpenAI and Kev are doubles that answer from each case's script."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import RateLimits
from app.audit.tracer import DatabaseAuditTracer
from app.contracts import (
    Confirmation,
    HandoffPacket,
    ModelSignals,
    ModelSource,
    ReasonCode,
    SessionContext,
    TraceRecord,
    TransactionRef,
)
from app.evaluation.harness.cases import EvalCase, seeded_cases
from app.evaluation.harness.faults import (
    Capture,
    FaultRegistry,
    HarnessDecision,
    HarnessLLM,
    HarnessOrchestrator,
    harness_tools,
)
from app.evaluation.harness.metrics import score
from app.evaluation.harness.runner import prediction_of, signal_turns, truth_of
from app.evaluation.harness.simulator import CustomerSimulator, Reply
from app.evaluation.scenarios import Scenario, load_scenarios
from app.evaluation.seed import build_rows, seed
from app.handoff import PolicyHandoffBuilder, SequenceHandoffIds
from app.handoff.queue import DatabaseHandoffQueue
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityService
from app.input_guard.service import RuleBasedInputGuard
from app.interfaces import ToolLayer
from app.main import create_app
from app.orchestrator.service import OrchestratorConfig
from app.orchestrator.state import InMemoryConversationStore
from app.policy import DeterministicPolicyEngine
from app.storage.models import Case
from app.templates.service import TemplateService
from app.tools import DatabaseToolLayer, ToolConfig
from tests.conftest import TEST_HASH_KEY, Pipeline
from tests.e2e.conftest import AS_OF, BUSINESS_DATE, CONFIG, PSEUDONYM_KEY, START, e2e_settings
from tests.identity.conftest import OTP, FakeClock, identity_config
from tests.orchestrator.fakes import ScriptedKev, ScriptedLLM, ext

KEYS = ("S001", "S090", "S091", "S092")
KEV = ModelSignals(
    source=ModelSource.KEV,
    reason_code_probs={ReasonCode.UNRECOGNIZED: 0.7, ReasonCode.INCORRECT_AMOUNT: 0.2},
    reason_code_other=0.1,
    ambiguity=0.4,
    escalation_risk=0.3,
)


class ApiChat:
    """The simulator's ChatClient over the FastAPI test client."""

    def __init__(self, client: TestClient) -> None:
        self._client = client

    async def login(self, document: str) -> str:
        assert (
            self._client.post("/auth/login", json={"document_number": document}).status_code == 202
        )
        verified = self._client.post("/auth/verify", json={"document_number": document, "otp": OTP})
        assert verified.status_code == 200
        return str(verified.json()["access_token"])

    async def turn(self, conversation_id: str, message: str, token: str | None) -> Reply:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = self._client.post(
            "/api/turn",
            json={"conversation_id": conversation_id, "message": message},
            headers=headers,
        )
        body = response.json()
        return Reply(
            response.status_code,
            body["conversation_id"],
            body["turn_index"],
            body["reply"],
            body["handed_off"],
            body["trace_id"],
        )

    async def logout(self, token: str) -> None:
        self._client.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})


class Traces:
    def __init__(self, tracer: DatabaseAuditTracer, queue: DatabaseHandoffQueue) -> None:
        self._tracer, self._queue = tracer, queue

    def trace(self, trace_id: str) -> TraceRecord | None:
        return self._tracer.get(trace_id)

    def handoff(self, handoff_id: str) -> HandoffPacket | None:
        return self._queue.get(handoff_id)


def script(llm: ScriptedLLM, kev: ScriptedKev, s: Scenario) -> None:
    """What the model doubles answer to each scripted message of the case."""
    d = s.disputes[0]
    assert d.transaction is not None
    ref = TransactionRef(transaction_id=s.transaction_id(d.transaction))
    language = s.language
    a = s.script.answers
    by_key: dict[str, Any] = {
        "transaction_ref": ext(language, transaction_ref=ref),
        "reason_code": ext(language, reason_code=d.reason_code),
        "card_in_possession": ext(language, card_in_possession=d.card_in_possession),
        "shared_credentials": ext(language, shared_credentials=d.shared_credentials),
        "expected_amount": ext(language, expected_amount=d.expected_amount),
        "block_offer": ext(language, confirmation=Confirmation.DECLINED),
        "summary": ext(language, confirmation=Confirmation.CONFIRMED),
        "authentication": ext(language),
    }
    llm.script[s.script.first] = ext(language, transaction_ref=ref, reason_code=d.reason_code)
    kev.signals_by_message[s.script.first] = KEV
    for key, text in a.items():
        if key in by_key:
            llm.script[text] = by_key[key]


@pytest.fixture
def world(loaded: Pipeline, db_session: Session, connection: Any) -> Iterator[dict[str, Any]]:
    sessions = sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )
    scenarios = load_scenarios()
    seed(db_session, build_rows(scenarios, BUSINESS_DATE, TEST_HASH_KEY, "test"))
    db_session.flush()
    clock = FakeClock(START)
    llm, kev = ScriptedLLM(tokens_per_call=0), ScriptedKev()
    for s in scenarios:
        if s.id in KEYS:
            script(llm, kev, s)
    faults, capture = FaultRegistry(), Capture()
    parameters = CONFIG.parameters
    tool_config = ToolConfig(as_of=AS_OF, parameters=parameters, retry_wait_seconds=0)
    tracer, queue = DatabaseAuditTracer(sessions), DatabaseHandoffQueue(sessions)

    def tools(session: SessionContext | None, conversation_id: str) -> ToolLayer:
        return DatabaseToolLayer(
            sessions, session, tool_config, conversation_id=conversation_id, clock=clock
        )

    identity = IdentityService(identity_config(), sessions, clock)
    orchestrator = HarnessOrchestrator(
        identity=identity,
        guard=RuleBasedInputGuard(sessions, parameters.INJECTION_STRIKES_MAX),
        llm=HarnessLLM(llm, faults, capture),
        decision=HarnessDecision(kev, faults, capture),
        engine=DeterministicPolicyEngine(CONFIG),
        tools=harness_tools(tools, faults, parameters.TOOL_MAX_RETRIES + 1),
        builder=PolicyHandoffBuilder(
            parameters, PSEUDONYM_KEY, SequenceHandoffIds(sessions), clock=clock
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
    cases = {c.key: c for c in seeded_cases(set(KEYS), scenarios)}
    with TestClient(app) as client:
        yield {
            "simulator": CustomerSimulator(ApiChat(client), Traces(tracer, queue)),
            "faults": faults,
            "capture": capture,
            "cases": cases,
            "db": db_session,
            "llm": llm,
        }


def play(world: dict[str, Any], key: str) -> Any:
    case: EvalCase = world["cases"][key]
    conversation_id = f"EV-test-{key}"
    world["faults"].register(conversation_id, case.faults)
    return asyncio.run(world["simulator"].play(case, "system-1", conversation_id))


def test_a_resolution_is_played_to_the_case(world: dict[str, Any]) -> None:
    result = play(world, "S001")
    assert result.error is None and result.end == "final"
    assert result.outcome == "RESOLVE" and result.actions == ["ACT-02", "ACT-04"]
    assert result.out_of_script == []
    case = world["cases"]["S001"]
    s = score(truth_of(case), prediction_of(result))
    assert s.full_ok and not s.unsafe and s.auto_resolved_ok
    assert [t.reply_kind for t in result.turns][-1] == "case_created"
    assert result.created_cases  # recorded for the cleanup of real customers


def test_a_write_failure_is_injected_for_its_case_only(world: dict[str, Any]) -> None:
    result = play(world, "S090")
    assert result.end == "handoff" and result.rules == ["ESC-10"]
    assert result.queue == "disputes"
    world["db"].expire_all()
    seeded = world["cases"]["S090"].customer_id
    assert not world["db"].scalars(select(Case).where(Case.customer_id == seeded)).all()
    other = play(world, "S001")  # the same Tool Layer, another conversation: no fault
    assert other.outcome == "RESOLVE"


def test_models_down_escalate_under_esc11(world: dict[str, Any]) -> None:
    calls_before = world["llm"].calls
    result = play(world, "S091")
    assert result.end == "handoff" and result.rules == ["ESC-11"]
    assert world["llm"].calls == calls_before  # the injected fault answered, not the model
    s = score(truth_of(world["cases"]["S091"]), prediction_of(result))
    assert s.full_ok


def test_an_injection_case_is_played_with_its_second_attempt(world: dict[str, Any]) -> None:
    result = play(world, "S092")
    assert result.end == "handoff" and result.rules == ["ESC-13"]
    assert result.turns[0].reply_kind == "ask_rephrase"
    s = score(truth_of(world["cases"]["S092"]), prediction_of(result))
    assert s.full_ok and s.priority_ok is None  # an interruption: priority not compared


def test_signals_are_captured_on_the_same_turns(world: dict[str, Any]) -> None:
    result = play(world, "S001")
    turns = signal_turns(world["cases"], [result], world["capture"])
    sources = {t.source for t in turns}
    assert sources == {"kev", "llm_fallback"}  # Kev answered the first message only
    first = next(t for t in turns if t.source == "llm_fallback")
    assert first.reason is ReasonCode.UNRECOGNIZED and first.escalate == 0
