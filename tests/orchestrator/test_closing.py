"""M12 manual test 3, last two defects: the block notice repeated after ACT-03, and a new
question after a final outcome (contract 22)."""

from __future__ import annotations

from decimal import Decimal

from app.contracts import Confirmation, Outcome, ReasonCode, SideQuestion, TransactionRef
from tests.orchestrator.fakes import REF_CAFE, World, build_world, ext

ALREADY_BLOCKED = "ya está bloqueada"
CLOSING_ES = "Con gusto. ¿Hay algo más en lo que pueda ayudarle?"
CLOSING_PT = "Por nada. Posso ajudar em mais alguma coisa?"
UNRECOGNIZED = "No reconozco el cargo de 50 dólares de Cafe Sintetico del 10 de junio"


def blocked_in_this_conversation(world: World) -> None:
    world.say(UNRECOGNIZED, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED))
    assert world.turn(UNRECOGNIZED).reply_kind == "block_offer"
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    blocked = world.turn("sí, confirmo")
    assert blocked.reply.startswith("Bloqueamos temporalmente su tarjeta ****4821.")


def resolved(world: World) -> None:
    blocked_in_this_conversation(world)
    for message, extraction in (
        ("sí, la tengo", ext(card_in_possession=True)),
        ("no, a nadie", ext(shared_credentials=False)),
        ("sí, confirmo los datos", ext(confirmation=Confirmation.CONFIRMED)),
    ):
        world.say(message, extraction)
        result = world.turn(message)
    assert result.outcome is Outcome.RESOLVE


# --- 1. The block notice ------------------------------------------------------------------------


def test_a_card_blocked_in_this_conversation_is_not_announced_again() -> None:
    world = build_world()
    blocked_in_this_conversation(world)
    world.say("sí, la tengo", ext(card_in_possession=True))
    next_turn = world.turn("sí, la tengo")
    assert ALREADY_BLOCKED not in next_turn.reply
    assert next_turn.reply_kind == "clarify:shared_credentials"


def test_nor_for_the_next_transaction_of_the_same_card() -> None:
    world = build_world()
    resolved(world)
    message = "También hay un cargo de Electro Mundo que no reconozco"
    ref = TransactionRef(merchant="Electro Mundo")
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    assert ALREADY_BLOCKED not in world.turn(message).reply


def test_a_card_blocked_before_the_conversation_is_still_announced() -> None:
    world = build_world()
    world.bank.blocked.add("PRD-1")
    world.say(UNRECOGNIZED, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED))
    assert world.turn(UNRECOGNIZED).reply.startswith("Su tarjeta ****4821 ya está bloqueada.")


# --- 2. Closing after a final outcome -----------------------------------------------------------


def test_thanks_after_resolve_closes_without_asking_for_a_transaction() -> None:
    world = build_world()
    resolved(world)
    for message in ("gracias", "ok", "listo"):
        world.say(message, ext())
        result = world.turn(message)
        assert result.reply == CLOSING_ES
        assert result.outcome is Outcome.RESOLVE  # the conversation keeps its result
    assert world.tracer.traces[-1].reply_kind == "closing"
    assert world.tracer.traces[-1].decisions == []


def test_another_charge_after_the_closing_starts_the_flow() -> None:
    world = build_world()
    resolved(world)
    world.say("gracias", ext())
    world.turn("gracias")
    message = "Ahora que lo pienso, hay otro cargo que no reconozco"
    world.say(message, ext(reason_code=ReasonCode.UNRECOGNIZED))
    assert world.turn(message).reply_kind == "clarify:transaction_ref"


def test_obrigado_after_inform_closes_in_portuguese() -> None:
    world = build_world()
    message = "Não reconheço uma cobrança do Kiosko 24 de 20 dólares"
    ref = TransactionRef(merchant="Kiosko 24", amount=Decimal("20"))
    world.say(message, ext("pt", transaction_ref=ref, reason_code=ReasonCode.NOT_RECEIVED))
    assert world.turn(message).outcome is Outcome.INFORM  # the charge is still pending
    for thanks in ("obrigado", "valeu"):
        world.say(thanks, ext("pt"))
        result = world.turn(thanks)
        assert result.reply == CLOSING_PT and result.outcome is Outcome.INFORM


def test_thanks_in_the_middle_of_the_flow_is_not_a_closing() -> None:
    world = build_world()
    world.say("Quiero disputar un cargo", ext(reason_code=ReasonCode.UNRECOGNIZED))
    world.turn("Quiero disputar un cargo")
    world.say("gracias", ext())
    assert world.turn("gracias").reply_kind != "closing"


def test_a_side_question_or_a_signal_after_resolve_is_not_a_closing() -> None:
    world = build_world()
    resolved(world)
    world.say("gracias, ¿y cuánto tarda?", ext(side=SideQuestion.TIMELINE))
    assert world.turn("gracias, ¿y cuánto tarda?").reply_kind != "closing"
    world.say("gracias, pero quiero hablar con alguien", ext(flags={"human_requested": True}))
    assert world.turn("gracias, pero quiero hablar con alguien").outcome is Outcome.ESCALATE


def test_the_closing_gets_no_connecting_sentence() -> None:
    world = build_world()
    resolved(world)
    world.llm.connect_sentence = "Gracias a usted."
    calls = len(world.llm.connect_modes)
    world.say("gracias", ext())
    assert world.turn("gracias").reply == CLOSING_ES
    assert len(world.llm.connect_modes) == calls


# --- 3. A question after a final outcome (contract 24) ------------------------------------------

CARD_BLOCKED_ES = "Sí, su tarjeta ****4821 está bloqueada temporalmente."


def test_card_status_after_resolve_is_answered_from_the_records_and_reopens_nothing() -> None:
    world = build_world()
    resolved(world)  # the card was blocked in this conversation (ACT-03)
    for question in ("Entonces ya está bloqueada?", "Mi tarjeta ya se encuentra bloqueada?"):
        world.say(question, ext(side=SideQuestion.CARD_STATUS))
        result = world.turn(question)
        assert result.reply == CARD_BLOCKED_ES
        assert result.reply_kind == "side:card_status"
        assert result.outcome is Outcome.RESOLVE  # the conversation keeps its result
        assert world.tracer.traces[-1].decisions == []  # the engine was not called
    world.say("Eso sería todo, gracias", ext())
    assert world.turn("Eso sería todo, gracias").reply == CLOSING_ES


def test_card_status_says_when_the_card_is_not_blocked() -> None:
    world = build_world()
    message = "¿Ya está bloqueada mi tarjeta?"
    world.say(message, ext(side=SideQuestion.CARD_STATUS))
    result = world.turn(message)
    assert result.reply.startswith("Su tarjeta ****4821 no está bloqueada.")


def test_card_status_without_a_session_only_asks_to_log_in() -> None:
    world = build_world()
    message = "¿Ya está bloqueada mi tarjeta?"
    world.say(message, ext(side=SideQuestion.CARD_STATUS))
    result = world.turn(message, token=None)
    assert result.reply_kind == "clarify:authentication"
    assert world.bank.reads == []  # nothing read before GATE-02


def test_a_side_question_after_inform_does_not_ask_for_a_transaction() -> None:
    world = build_world()
    message = "No me llegó la compra de Kiosko 24 de 20 dólares"
    ref = TransactionRef(merchant="Kiosko 24", amount=Decimal("20"))
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.NOT_RECEIVED))
    assert world.turn(message).outcome is Outcome.INFORM  # the charge is still pending
    world.say("¿cuánto tarda?", ext(side=SideQuestion.TIMELINE))
    result = world.turn("¿cuánto tarda?")
    assert result.reply.startswith("Una vez registrada, nuestro equipo revisa la disputa")
    assert result.reply_kind == "side:timeline" and "transacción" not in result.reply
    assert result.outcome is Outcome.INFORM


def test_a_side_question_with_a_new_detail_after_resolve_still_goes_on() -> None:
    world = build_world()
    resolved(world)
    message = "¿cuánto tarda? Además hay otro cargo de Electro Mundo que no reconozco"
    ref = TransactionRef(merchant="Electro Mundo")
    world.say(
        message,
        ext(side=SideQuestion.TIMELINE, transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED),
    )
    assert world.turn(message).reply_kind != "side:timeline"
