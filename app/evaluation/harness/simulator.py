"""The customer simulator (M18): plays one case's script against the chat API.

The customer logs in with the case's document and the test OTP (unless the case is about not
authenticating), sends the first message, and then answers what each reply asks, read from the
turn's trace: ``clarify:<target>`` gets the script's answer for that target, ``summary`` and
``block_offer`` theirs, ``ask_rephrase`` the second attempt of an injection case. A side
question is sent once, at the first question. A question without a prepared answer gets a
neutral reply, recorded as out of script. The conversation ends at a handoff, at a final
outcome (or the next dispute of the script), or after ``MAX_TURNS`` turns.

An expired session is simulated by logging out after the first reply; the customer logs in
again when the assistant asks for authentication. When the assistant shows candidate
transactions, the customer picks the case's own ("sí, es esa", or its number), or says it is none
of them, as a customer would.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Protocol

import httpx

from app.contracts import ActionId, HandoffPacket, ToolStatus, TraceRecord
from app.evaluation.harness.cases import EvalCase

MAX_TURNS = 15
NEUTRAL = {
    "es": "No sé qué responder a eso.",
    "pt": "Não sei o que responder.",
    "en": "I don't know what to answer.",
}
FINAL_KINDS = ("case_created", "inform:", "refuse", "closing")  # automation gave its answer
PICK_ONE = {"es": "Sí, es esa", "pt": "Sim, é essa", "en": "Yes, that one"}
PICK_NUMBER = {"es": "Es la número {n}", "pt": "É a número {n}", "en": "It is number {n}"}
NONE_OF_THEM = {"es": "No, no es ninguna de esas", "pt": "Não, não é nenhuma dessas",
                "en": "No, none of those"}  # fmt: skip
ACTIONS_OF = {
    ActionId.CREATE_CASE: ("ACT-02", "ACT-04"),  # the case carries the credit flag (ACT-04)
    ActionId.BLOCK_CARD: ("ACT-03",),
    ActionId.TRANSFER_TO_HUMAN: ("ACT-05",),
}


@dataclass(frozen=True)
class Reply:
    status: int
    conversation_id: str
    turn_index: int
    reply: str
    handed_off: bool
    trace_id: str | None


class ChatClient(Protocol):
    async def login(self, document: str) -> str: ...

    async def turn(self, conversation_id: str, message: str, token: str | None) -> Reply: ...

    async def logout(self, token: str) -> None: ...


class TraceSource(Protocol):
    def trace(self, trace_id: str) -> TraceRecord | None: ...

    def handoff(self, handoff_id: str) -> HandoffPacket | None: ...


class LoginError(RuntimeError):
    """The simulated customer could not log in."""


class HttpChatClient:
    """The real API: ``/auth/login``, ``/auth/verify``, ``/api/turn``, ``/auth/logout``."""

    def __init__(self, http: httpx.AsyncClient, otp: str) -> None:
        self._http, self._otp = http, otp

    async def login(self, document: str) -> str:
        sent = await self._http.post("/auth/login", json={"document_number": document})
        if sent.status_code != 202:
            raise LoginError(f"login answered {sent.status_code}")
        verified = await self._http.post(
            "/auth/verify", json={"document_number": document, "otp": self._otp}
        )
        if verified.status_code != 200:
            raise LoginError(f"verify answered {verified.status_code}")
        return str(verified.json()["access_token"])

    async def turn(self, conversation_id: str, message: str, token: str | None) -> Reply:
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        response = await self._http.post(
            "/api/turn",
            json={"conversation_id": conversation_id, "message": message},
            headers=headers,
        )
        if response.status_code != 200:
            return Reply(response.status_code, conversation_id, -1, "", False, None)
        body = response.json()
        return Reply(
            200,
            body["conversation_id"],
            body["turn_index"],
            body["reply"],
            body["handed_off"],
            body["trace_id"],
        )

    async def logout(self, token: str) -> None:
        await self._http.post("/auth/logout", headers={"Authorization": f"Bearer {token}"})


@dataclass
class TurnRecord:
    index: int
    reply_kind: str
    outcome: str | None
    server_ms: float | None  # the trace's total latency
    wall_ms: float  # as the customer saw it
    cost_usd: Decimal | None
    out_of_script: bool  # the message this turn answered with was the neutral one
    clarify_target: str | None = None  # what the turn's decision asked for, if it asked


@dataclass
class ConversationResult:
    key: str
    run: str
    conversation_id: str
    turns: list[TurnRecord] = field(default_factory=list)
    end: str = ""  # handoff | final | max_turns | error
    outcome: str | None = None
    rules: list[str] = field(default_factory=list)
    queue: str | None = None
    priority: str | None = None
    failed_gates: list[str] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)  # executed and verified
    out_of_script: list[str] = field(default_factory=list)  # reply kinds without an answer
    error: str | None = None
    model_versions: list[str] = field(default_factory=list)
    prompt_versions: list[str] = field(default_factory=list)
    created_cases: list[str] = field(default_factory=list, repr=False)  # in memory: cleanup

    @property
    def handed_off(self) -> bool:
        return self.end == "handoff"

    @property
    def cost_usd(self) -> Decimal | None:
        costs = [t.cost_usd for t in self.turns]
        if not costs or any(c is None for c in costs):
            return None
        return sum((c for c in costs if c is not None), Decimal(0))


def pick(language: str, candidates: list[str], target: str | None) -> str:
    """How a customer answers a list of candidate transactions: their own, or none."""
    if target is None or target not in candidates:
        return NONE_OF_THEM.get(language, NONE_OF_THEM["es"])
    if len(candidates) == 1:
        return PICK_ONE.get(language, PICK_ONE["es"])
    template = PICK_NUMBER.get(language, PICK_NUMBER["es"])
    return template.format(n=candidates.index(target) + 1)


def answer_key(kind: str) -> str | None:
    """The script answer a reply kind asks for."""
    if kind.startswith("clarify:"):
        return kind.split(":", 1)[1]
    if kind in ("summary", "block_offer", "ask_rephrase"):
        return kind
    return None


class CustomerSimulator:
    def __init__(self, chat: ChatClient, traces: TraceSource, max_turns: int = MAX_TURNS) -> None:
        self._chat, self._traces, self._max_turns = chat, traces, max_turns

    async def play(self, case: EvalCase, run: str, conversation_id: str) -> ConversationResult:
        result = ConversationResult(case.key, run, conversation_id)
        try:
            await self._play(case, result)
        except Exception as exc:  # recorded as a failed conversation, never raised
            result.end, result.error = "error", f"{type(exc).__name__}: {exc}"
        return result

    async def _play(self, case: EvalCase, result: ConversationResult) -> None:
        answers = case.script.answers
        token = await self._login(case) if case.logs_in else None
        message, neutral = case.script.first, False
        sides = list(case.script.side_questions)
        next_dispute, last_key = 2, None
        for index in range(self._max_turns):
            started = time.perf_counter()
            reply = await self._chat.turn(result.conversation_id, message, token)
            wall = (time.perf_counter() - started) * 1000
            if reply.status != 200 or reply.trace_id is None:
                result.end, result.error = "error", f"turn answered {reply.status}"
                return
            trace = self._traces.trace(reply.trace_id)
            if trace is None:
                result.end, result.error = "error", "trace not found"
                return
            self._record(result, trace, wall, neutral, index)
            kind = trace.reply_kind or ""
            if reply.handed_off:
                result.end = "handoff"
                self._handoff(result, trace)
                return
            if kind.startswith(FINAL_KINDS):
                follow = answers.get(f"transaction_ref_{next_dispute}")
                if follow is None:
                    result.end = "final"
                    return
                message, neutral, next_dispute = follow, False, next_dispute + 1
                continue
            if case.conditions.expired_session and index == 0 and token is not None:
                await self._chat.logout(token)  # the session expires mid-way
                token = None
            key = answer_key(kind) or (last_key if kind.startswith("side:") else None)
            last_key = key or last_key
            if sides and kind.startswith("clarify:"):
                message, neutral = sides.pop(0), False  # the question stays pending
                continue
            shown = trace.decisions[-1].candidate_transaction_ids if trace.decisions else []
            if kind == "clarify:transaction_ref" and shown:
                dispute = next_dispute - 2  # the dispute in progress
                target = case.targets[dispute] if dispute < len(case.targets) else None
                message, neutral = pick(case.language, list(shown), target), False
                continue
            if key == "authentication" and case.logs_in and token is None:
                token = await self._login(case)
            text = answers.get(key) if key else None
            if text is None and key == "confirmation":
                text = answers.get("summary")
            if text is None:
                result.out_of_script.append(kind or "unknown")
                text, neutral = NEUTRAL.get(case.language, NEUTRAL["es"]), True
            else:
                neutral = False
            message = text
        result.end = "max_turns"

    async def _login(self, case: EvalCase) -> str:
        if case.document is None:
            raise LoginError(f"{case.key} has no document")
        return await self._chat.login(case.document)

    def _record(
        self,
        result: ConversationResult,
        trace: TraceRecord,
        wall: float,
        neutral: bool,
        index: int,
    ) -> None:
        result.turns.append(
            TurnRecord(
                index=index,
                reply_kind=trace.reply_kind or "",
                outcome=trace.outcome.value if trace.outcome else None,
                server_ms=trace.total_latency_ms,
                wall_ms=wall,
                cost_usd=trace.estimated_cost_usd,
                out_of_script=neutral,
                clarify_target=(
                    trace.decisions[-1].clarify_target.value
                    if trace.decisions and trace.decisions[-1].clarify_target
                    else None
                ),
            )
        )
        result.outcome = trace.outcome.value if trace.outcome else result.outcome
        if trace.decisions:
            last = trace.decisions[-1]
            result.failed_gates = [g.gate_id for g in last.gates_evaluated if not g.passed]
        for call in trace.tool_calls:
            if call.status is ToolStatus.SUCCESS:
                for action in ACTIONS_OF.get(call.action, ()):
                    if action not in result.actions:
                        result.actions.append(action)
                if call.action is ActionId.CREATE_CASE and call.record_id:
                    result.created_cases.append(call.record_id)
        for model_call in trace.model_calls:
            version = f"{model_call.provider}:{model_call.response_model or model_call.model}"
            if version not in result.model_versions:
                result.model_versions.append(version)
            if (
                model_call.prompt_version
                and model_call.prompt_version not in result.prompt_versions
            ):
                result.prompt_versions.append(model_call.prompt_version)

    def _handoff(self, result: ConversationResult, trace: TraceRecord) -> None:
        packet = self._traces.handoff(trace.handoff_id) if trace.handoff_id else None
        result.outcome = "ESCALATE"
        if packet is not None:
            result.rules = list(packet.triggered_rules)
            result.queue, result.priority = packet.queue.value, packet.priority.value
