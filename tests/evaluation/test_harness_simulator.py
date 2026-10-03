"""The customer simulator against a scripted chat double, and its HTTP client."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.contracts import (
    ActionId,
    HandoffPacket,
    Outcome,
    ToolResult,
    ToolStatus,
    TraceRecord,
)
from app.evaluation.harness.cases import EvalCase
from app.evaluation.harness.simulator import (
    NEUTRAL,
    CustomerSimulator,
    HttpChatClient,
    LoginError,
    Reply,
    answer_key,
)
from app.evaluation.labeler import CaseLabel
from app.evaluation.scenarios import Conditions, Path3, ScriptSpec

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)


def case(answers: dict[str, str], *, sides: tuple[str, ...] = (), **conditions: Any) -> EvalCase:
    label = CaseLabel(
        case_id="S001",
        language="es",
        path=Path3.RESOLUTION,
        outcome=Outcome.RESOLVE,
        triggered_rules=[],
        disputes=[],
    )
    return EvalCase(
        key="S001",
        title="t",
        language="es",
        path="resolution",
        data_source="seeded",
        label=label,
        script=ScriptSpec(first="hola", answers=answers, side_questions=list(sides)),
        conditions=Conditions(**conditions),
        reasons=(None,),
        customer_id="SEED-C001",
        document="SEED-0001",
    )


@dataclass
class Step:
    kind: str
    outcome: str = "CLARIFY"
    handed_off: bool = False
    tools: list[ToolResult] = field(default_factory=list)


@dataclass
class Bot:
    """A chat whose replies follow a list of steps; it records what the customer sent."""

    steps: list[Step]
    sent: list[tuple[str, str | None]] = field(default_factory=list)
    logins: int = 0
    logouts: int = 0
    traces: dict[str, TraceRecord] = field(default_factory=dict)
    status: int = 200

    async def login(self, document: str) -> str:
        self.logins += 1
        return f"token-{self.logins}"

    async def logout(self, token: str) -> None:
        self.logouts += 1

    async def turn(self, conversation_id: str, message: str, token: str | None) -> Reply:
        index = len(self.sent)
        self.sent.append((message, token))
        if self.status != 200:
            return Reply(self.status, conversation_id, -1, "", False, None)
        step = self.steps[min(index, len(self.steps) - 1)]
        trace_id = f"TRC-{index}"
        self.traces[trace_id] = TraceRecord(
            trace_id=trace_id,
            conversation_id=conversation_id,
            turn_index=index,
            created_at=NOW,
            outcome=Outcome(step.outcome),
            reply_kind=step.kind,
            tool_calls=step.tools,
            handoff_id="HO-20261003-000001" if step.handed_off else None,
            policy_version="0.4.11",
            total_latency_ms=100.0,
            estimated_cost_usd=Decimal("0.001"),
        )
        return Reply(200, conversation_id, index, "texto", step.handed_off, trace_id)

    def trace(self, trace_id: str) -> TraceRecord | None:
        return self.traces.get(trace_id)

    def handoff(self, handoff_id: str) -> HandoffPacket | None:
        return None


def play(bot: Bot, c: EvalCase, max_turns: int = 15) -> Any:
    return asyncio.run(CustomerSimulator(bot, bot, max_turns).play(c, "r1", "EV-r1-S001"))


CASE_CREATED = ToolResult(
    action=ActionId.CREATE_CASE,
    status=ToolStatus.SUCCESS,
    verified=True,
    attempts=1,
    record_id="DSP-1",
    idempotency_key="SEED-T001-t1:RC_FEE",
    completed_at=NOW,
)


def test_answers_follow_the_reply_kind() -> None:
    bot = Bot(
        [
            Step("clarify:transaction_ref"),
            Step("clarify:reason_code"),
            Step("summary"),
            Step("case_created", "RESOLVE", tools=[CASE_CREATED]),
        ]
    )
    result = play(
        bot,
        case(
            {"transaction_ref": "la compra", "reason_code": "no la hice", "summary": "sí, confirmo"}
        ),
    )
    assert [m for m, _ in bot.sent] == ["hola", "la compra", "no la hice", "sí, confirmo"]
    assert all(token == "token-1" for _, token in bot.sent)
    assert result.end == "final" and result.outcome == "RESOLVE"
    assert result.actions == ["ACT-02", "ACT-04"] and result.created_cases == ["DSP-1"]
    assert result.cost_usd == Decimal("0.004") and result.out_of_script == []


def test_an_unprepared_question_gets_the_neutral_reply() -> None:
    bot = Bot([Step("clarify:merchant_contacted"), Step("inform:merchant_not_contacted", "INFORM")])
    result = play(bot, case({}))
    assert bot.sent[1][0] == NEUTRAL["es"] and result.out_of_script == [
        "clarify:merchant_contacted"
    ]
    assert result.turns[1].out_of_script and not result.turns[0].out_of_script


def test_the_turn_limit() -> None:
    bot = Bot([Step("clarify:transaction_ref")])
    result = play(bot, case({"transaction_ref": "no sé"}), max_turns=4)
    assert result.end == "max_turns" and len(bot.sent) == 4


def test_a_side_question_is_sent_once_at_the_first_question() -> None:
    bot = Bot(
        [
            Step("clarify:reason_code"),
            Step("side:refund"),
            Step("clarify:reason_code"),
            Step("inform:dispute_withdrawn", "INFORM"),
        ]
    )
    play(bot, case({"reason_code": "no la hice"}, sides=("¿me devuelven el dinero?",)))
    assert [m for m, _ in bot.sent] == [
        "hola",
        "¿me devuelven el dinero?",
        "no la hice",
        "no la hice",
    ]


def test_an_expired_session_logs_in_again_when_asked() -> None:
    bot = Bot(
        [
            Step("clarify:reason_code"),
            Step("clarify:authentication"),
            Step("inform:transaction_pending", "INFORM"),
        ]
    )
    play(
        bot, case({"reason_code": "no la hice", "authentication": "ya entré"}, expired_session=True)
    )
    assert bot.logouts == 1 and bot.logins == 2
    assert [t for _, t in bot.sent] == ["token-1", None, "token-2"]


def test_without_authentication_the_customer_never_logs_in() -> None:
    bot = Bot([Step("clarify:authentication"), Step("inform:authentication_declined", "INFORM")])
    play(
        bot,
        case({"authentication": "no quiero"}, authenticated=False, authentication_declined=True),
    )
    assert bot.logins == 0 and bot.sent[1] == ("no quiero", None)


def test_the_next_dispute_of_the_script_follows_a_final_answer() -> None:
    bot = Bot([Step("case_created", "RESOLVE"), Step("handoff", "ESCALATE", handed_off=True)])
    result = play(bot, case({"transaction_ref_2": "y otro cargo"}))
    assert bot.sent[1][0] == "y otro cargo" and result.end == "handoff"
    assert result.outcome == "ESCALATE" and result.rules == []  # no packet in this double


def test_a_hedged_confirmation_and_the_summary_fallback() -> None:
    assert answer_key("clarify:confirmation") == "confirmation"
    bot = Bot([Step("summary"), Step("clarify:confirmation"), Step("case_created", "RESOLVE")])
    play(bot, case({"summary": "creo que sí"}))
    assert bot.sent[2][0] == "creo que sí"  # no separate answer: the summary's again


def test_failures_are_recorded_never_raised() -> None:
    bot = Bot([Step("clarify:x")], status=500)
    result = play(bot, case({}))
    assert result.end == "error" and "500" in (result.error or "")
    missing = case({})
    nobody = EvalCase(**{**missing.__dict__, "document": None})
    assert "LoginError" in (play(Bot([Step("clarify:x")]), nobody).error or "")
    assert answer_key("block_offer") == "block_offer" and answer_key("handoff") is None


def test_the_http_client_speaks_the_api() -> None:
    seen: list[tuple[str, dict[str, Any], str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.url.path, body, request.headers.get("authorization")))
        if request.url.path == "/auth/login":
            return httpx.Response(202, json={"status": "otp_sent", "expires_in_seconds": 300})
        if request.url.path == "/auth/verify":
            return httpx.Response(200, json={"access_token": "tok"})
        if request.url.path == "/api/turn":
            if body["message"] == "boom":
                return httpx.Response(429)
            return httpx.Response(
                200,
                json={
                    "conversation_id": body["conversation_id"],
                    "turn_index": 0,
                    "reply": "ok",
                    "handed_off": False,
                    "trace_id": "TRC-1",
                },
            )
        return httpx.Response(204)

    async def scenario() -> tuple[str, Reply, Reply]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://x"
        ) as http:
            chat = HttpChatClient(http, "123456")
            token = await chat.login("SEED-0001")
            reply = await chat.turn("EV-1", "hola", token)
            failed = await chat.turn("EV-1", "boom", None)
            await chat.logout(token)
            return token, reply, failed

    token, reply, failed = asyncio.run(scenario())
    assert token == "tok" and reply.trace_id == "TRC-1" and failed.status == 429
    assert seen[1][1] == {"document_number": "SEED-0001", "otp": "123456"}
    assert seen[2][2] == "Bearer tok" and seen[3][2] is None and seen[4][0] == "/auth/logout"


def test_a_refused_login_raises() -> None:
    async def scenario() -> None:
        transport = httpx.MockTransport(lambda request: httpx.Response(429))
        async with httpx.AsyncClient(transport=transport, base_url="http://x") as http:
            await HttpChatClient(http, "1").login("SEED-0001")

    with pytest.raises(LoginError, match="429"):
        asyncio.run(scenario())

    async def verify_fails() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(202 if request.url.path == "/auth/login" else 401)

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler), base_url="http://x"
        ) as http:
            await HttpChatClient(http, "1").login("SEED-0001")

    with pytest.raises(LoginError, match="401"):
        asyncio.run(verify_fails())


def test_the_customer_picks_their_transaction_from_the_candidates() -> None:
    from app.evaluation.harness.simulator import pick

    assert pick("es", ["T1"], "T1") == "Sí, es esa"
    assert pick("pt", ["T0", "T1", "T2"], "T2") == "É a número 3"
    assert pick("es", ["T0"], "T9") == "No, no es ninguna de esas"
    assert pick("pt", ["T0"], None) == "Não, não é nenhuma dessas"


def test_candidates_shown_are_answered_with_a_pick() -> None:
    from app.contracts import ClarifyTarget, PolicyDecision

    @dataclass
    class Shown(Bot):
        def __post_init__(self) -> None:
            self.decision = PolicyDecision(
                outcome=Outcome.CLARIFY,
                policy_version="0.4.12",
                clarify_target=ClarifyTarget.TRANSACTION_REF,
                candidate_transaction_ids=["SEED-T001-t0", "SEED-T001-t1"],
            )

        async def turn(self, conversation_id: str, message: str, token: str | None) -> Reply:
            reply = await super().turn(conversation_id, message, token)
            trace = self.traces[reply.trace_id or ""]
            if trace.reply_kind == "clarify:transaction_ref":
                self.traces[reply.trace_id or ""] = trace.model_copy(
                    update={"decisions": [self.decision]}
                )
            return reply

    bot = Shown([Step("clarify:transaction_ref"), Step("case_created", "RESOLVE")])
    picked = EvalCase(
        **{**case({"transaction_ref": "la compra"}).__dict__, "targets": ("SEED-T001-t1",)}
    )
    result = play(bot, picked)
    assert bot.sent[1][0] == "Es la número 2" and result.out_of_script == []
