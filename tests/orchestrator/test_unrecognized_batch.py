"""Policy §7 (v0.4.11): the ESC-03 batch counts the unrecognized charges the customer reported
in the conversation. The count the engine sees is the larger of the reported and the evaluated
ones, never falls between turns, and falls back to the evaluated ones when extract fails."""

from __future__ import annotations

from app.contracts import PolicyDecision, PolicyRequest, ReasonCode
from app.policy import DeterministicPolicyEngine
from tests.orchestrator.fakes import CONFIG, REF_CAFE, World, build_world, ext


class RecordingEngine(DeterministicPolicyEngine):
    """The real engine, keeping every request it evaluates."""

    def __init__(self) -> None:
        super().__init__(CONFIG)
        self.requests: list[PolicyRequest] = []

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        self.requests.append(request)
        return super().evaluate(request)


def recording_world() -> tuple[World, RecordingEngine]:
    engine = RecordingEngine()
    return build_world(engine_factory=lambda: engine), engine


def counted(engine: RecordingEngine) -> int:
    return engine.requests[-1].counters.unrecognized_transactions


def test_the_reported_charges_count_before_any_is_evaluated() -> None:
    world, engine = recording_world()
    world.say("No reconozco dos cargos", ext(reason_code=ReasonCode.UNRECOGNIZED, unrecognized=2))
    reply = world.turn("No reconozco dos cargos")
    assert counted(engine) == 2  # not 1: both charges count from the first message
    assert reply.outcome == "CLARIFY"  # below UNRECOGNIZED_BATCH_MAX (3)


def test_the_count_never_falls_between_turns() -> None:
    world, engine = recording_world()
    world.say("No reconozco dos cargos", ext(reason_code=ReasonCode.UNRECOGNIZED, unrecognized=2))
    world.turn("No reconozco dos cargos")
    world.say("Uno es el de Cafe Sintetico", ext(transaction_ref=REF_CAFE, unrecognized=0))
    world.turn("Uno es el de Cafe Sintetico")
    assert counted(engine) == 2  # the message reported none, the conversation still has two


def test_three_reported_charges_escalate_in_the_first_turn() -> None:
    world, engine = recording_world()
    message = "No reconozco tres cargos de mi tarjeta"
    world.say(message, ext(reason_code=ReasonCode.UNRECOGNIZED, unrecognized=3))
    world.turn(message)
    assert counted(engine) == 3
    assert any("ESC-03" in request_rules for request_rules in _rules(world))


def test_without_extract_only_the_evaluated_charges_count() -> None:
    world, engine = recording_world()
    world.llm.fail = True  # extract unavailable: nothing reported, the current behavior
    world.turn("No reconozco tres cargos de mi tarjeta")
    assert counted(engine) == 0


def _rules(world: World) -> list[list[str]]:
    return [list(d.triggered_rules) for trace in world.tracer.traces for d in trace.decisions]
