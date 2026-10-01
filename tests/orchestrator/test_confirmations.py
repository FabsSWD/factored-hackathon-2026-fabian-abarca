"""COM-03 confirmations, the card block, corrections, ESC-05 and ESC-03 (decisions 5 to 8)."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from app.contracts import (
    ActionId,
    ClarifyTarget,
    Confirmation,
    Outcome,
    ReasonCode,
    TransactionRef,
)
from tests.orchestrator.fakes import REF_CAFE, World, build_world, ext

AMOUNT_MESSAGE = "Me cobraron 50 en Cafe Sintetico el 10 de junio y acordamos 40"


def at_summary(world: World) -> None:
    world.say(
        AMOUNT_MESSAGE,
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    assert world.turn(AMOUNT_MESSAGE).reply_kind == "summary"


def state(world: World, conversation_id: str = "CONV-1"):  # type: ignore[no-untyped-def]
    return world.orchestrator._store.get(conversation_id)


# --- Confirmation ---------------------------------------------------------------------------------


def test_hedged_reply_does_not_confirm_and_is_asked_once_more() -> None:
    world = build_world()
    at_summary(world)
    assert state(world).counters.total_clarifications == 0  # the first summary is not one
    world.say("creo que sí", ext(confirmation=Confirmation.HEDGED))
    hedged = world.turn("creo que sí")
    assert hedged.reply_kind == "clarify:confirmation"
    assert world.bank.cases == []
    assert state(world).counters.clarifications_by_slot[ClarifyTarget.CONFIRMATION] == 1
    assert state(world).slots.confirmation is None  # cleared after use
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert world.turn("sí, confirmo").outcome is Outcome.RESOLVE


def test_portuguese_confirmation() -> None:
    world = build_world()
    message = "Cobraram 50 dólares no Cafe Sintetico dia 10 de junho, combinamos 40"
    world.say(
        message,
        ext(
            "pt",
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    summary = world.turn(message)
    assert "sim, confirmo" in summary.reply and "USD 50,00" in summary.reply
    world.say("sim, confirmo", ext("pt", confirmation=Confirmation.CONFIRMED))
    done = world.turn("sim, confirmo")
    assert done.outcome is Outcome.RESOLVE and "Registramos sua contestação" in done.reply


def test_a_slot_change_after_the_summary_clears_the_confirmation() -> None:
    world = build_world()
    at_summary(world)
    world.say(
        "sí, confirmo, aunque eran 45",
        ext(confirmation=Confirmation.CONFIRMED, expected_amount=Decimal("45")),
    )
    result = world.turn("sí, confirmo, aunque eran 45")
    assert result.reply_kind == "summary"  # shown again with the new value
    assert world.bank.cases == []


def test_withdrawn_at_the_summary_ends_without_case() -> None:
    world = build_world()
    at_summary(world)
    world.say("mejor no, déjelo así", ext(confirmation=Confirmation.WITHDRAWN))
    result = world.turn("mejor no, déjelo así")
    assert result.reply_kind == "inform:dispute_withdrawn"
    assert "offer_transfer" not in result.reply and "agente" in result.reply
    assert world.bank.cases == []


# --- Corrections (corrections.py) -----------------------------------------------------------------


def test_declined_with_a_new_amount_rematches_the_transaction() -> None:
    world = build_world()
    at_summary(world)
    world.say(
        "no, el monto era 18,90 de Streaming Plus",
        ext(
            confirmation=Confirmation.DECLINED,
            transaction_ref=TransactionRef(amount=Decimal("18.90"), merchant="Streaming Plus"),
        ),
    )
    result = world.turn("no, el monto era 18,90 de Streaming Plus")
    # GATE-05 runs again: two Streaming Plus charges of 18.90 are listed.
    assert result.reply_kind == "clarify:transaction_ref"


def test_declined_with_another_reason_is_asked_not_replaced() -> None:
    world = build_world()
    at_summary(world)
    world.say(
        "no, el monto está mal, eran 40",
        ext(
            confirmation=Confirmation.DECLINED,
            reason_code=ReasonCode.DUPLICATE,
            expected_amount=Decimal("40"),
        ),
    )
    result = world.turn("no, el monto está mal, eran 40")
    assert result.reply_kind == "clarify:reason_code"
    assert state(world).slots.reason_code is ReasonCode.INCORRECT_AMOUNT


def test_declined_without_details_asks_which_detail_is_wrong() -> None:
    world = build_world()
    at_summary(world)
    world.say("no, eso no es correcto", ext(confirmation=Confirmation.DECLINED))
    result = world.turn("no, eso no es correcto")
    assert result.reply_kind == "clarify:correction"
    world.say("mejor no", ext(confirmation=Confirmation.WITHDRAWN))
    assert world.turn("mejor no").reply_kind == "inform:dispute_withdrawn"


# --- Card block (ACT-03) --------------------------------------------------------------------------

UNRECOGNIZED = "No reconozco el cargo de 50 dólares de Cafe Sintetico del 10 de junio"


def unrecognized(world: World, **slots: Any) -> None:
    world.say(
        UNRECOGNIZED, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED, **slots)
    )


def test_block_is_confirmed_on_its_own_before_the_summary() -> None:
    world = build_world()
    unrecognized(world, card_in_possession=True, shared_credentials=False)
    offer = world.turn(UNRECOGNIZED)
    assert offer.reply_kind == "block_offer"
    assert "bloquear temporalmente su tarjeta ****4821" in offer.reply
    assert state(world).counters.total_clarifications == 0  # not a clarification
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    after = world.turn("sí, confirmo")
    assert world.bank.blocked == {"PRD-1"}
    assert after.reply.startswith("Bloqueamos temporalmente su tarjeta ****4821.")
    assert after.reply_kind == "summary"  # the block's yes did not confirm the dispute
    assert world.bank.cases == []


def test_declining_the_block_does_not_affect_the_dispute() -> None:
    world = build_world()
    unrecognized(world, card_in_possession=True, shared_credentials=False)
    world.turn(UNRECOGNIZED)
    world.say("no, no la bloquee", ext(confirmation=Confirmation.DECLINED))
    after = world.turn("no, no la bloquee")
    assert world.bank.blocked == set()
    assert after.reply_kind == "summary"
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert world.turn("sí, confirmo").outcome is Outcome.RESOLVE


def test_unclear_answer_to_the_block_is_asked_once_more() -> None:
    world = build_world()
    unrecognized(world, card_in_possession=True, shared_credentials=False)
    world.turn(UNRECOGNIZED)
    world.say("mmm no sé", ext(confirmation=Confirmation.HEDGED))
    assert world.turn("mmm no sé").reply_kind == "block_offer"
    world.say("tal vez", ext(confirmation=Confirmation.HEDGED))
    after = world.turn("tal vez")
    assert after.reply_kind == "summary" and world.bank.blocked == set()


def test_failed_block_escalates_with_tool_failure() -> None:
    world = build_world()
    world.bank.block_failure = True
    unrecognized(world, card_in_possession=True, shared_credentials=False)
    world.turn(UNRECOGNIZED)
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    result = world.turn("sí, confirmo")
    assert result.outcome is Outcome.ESCALATE
    assert result.reply.startswith("No pude confirmar que la acción se haya completado.")
    (packet,) = world.bank.packets
    assert "ESC-10" in packet.triggered_rules
    assert packet.actions_taken[0].action is ActionId.BLOCK_CARD


def test_esc03_offers_the_block_first_then_hands_off() -> None:
    world = build_world()
    message = "Me robaron el celular y veo un cargo de 50 en Cafe Sintetico el 10 de junio"
    world.say(
        message,
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.UNRECOGNIZED,
            flags={"account_takeover_reported": True},
            claims=["Le robaron el celular"],
        ),
    )
    first = world.turn(message)
    assert first.reply_kind == "block_offer" and not first.closed
    assert world.bank.packets == []
    assert state(world).counters.total_clarifications == 0
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    second = world.turn("sí, confirmo")
    assert second.outcome is Outcome.ESCALATE and second.closed
    (packet,) = world.bank.packets
    assert packet.triggered_rules == ["ESC-03"]
    assert packet.actions_taken[0].action is ActionId.BLOCK_CARD
    assert packet.escalation_reasons[0].evidence[0].claims == ["Le robaron el celular"]


def test_esc05_is_honored_at_once_and_the_pending_block_goes_to_the_agent() -> None:
    world = build_world()
    unrecognized(world, card_in_possession=True, shared_credentials=False)
    world.turn(UNRECOGNIZED)  # block offered
    world.say("quiero hablar con una persona", ext(flags={"human_requested": True}))
    result = world.turn("quiero hablar con una persona")
    assert result.outcome is Outcome.ESCALATE and result.closed
    (packet,) = world.bank.packets
    assert any("card block of ****4821" in q for q in packet.open_questions)
    assert world.bank.blocked == set()


def test_esc05_before_the_block_is_offered_also_reaches_the_agent() -> None:
    world = build_world()
    unrecognized(
        world, card_in_possession=True, shared_credentials=False, flags={"human_requested": True}
    )
    result = world.turn(UNRECOGNIZED)
    assert result.outcome is Outcome.ESCALATE
    (packet,) = world.bank.packets
    assert any("card block of ****4821" in q for q in packet.open_questions)


def test_card_already_blocked_is_told_once() -> None:
    world = build_world()
    world.bank.blocked.add("PRD-1")
    unrecognized(world, card_in_possession=False, shared_credentials=False)
    result = world.turn(UNRECOGNIZED)
    assert result.reply.startswith("Su tarjeta ****4821 ya está bloqueada.")
    assert result.reply_kind == "summary"


# --- Duplicate charge -----------------------------------------------------------------------------


def test_duplicate_yes_fills_duplicate_ref() -> None:
    world = build_world()
    message = "Me cobraron dos veces Streaming Plus, el segundo cargo"
    world.say(
        message,
        ext(
            transaction_ref=TransactionRef(transaction_id="TXN-5"), reason_code=ReasonCode.DUPLICATE
        ),
    )
    asked = world.turn(message)
    assert asked.reply_kind == "clarify:duplicate_ref"
    assert "15/06/2026" in asked.reply
    world.say("sí, ese es", ext(confirmation=Confirmation.CONFIRMED))
    summary = world.turn("sí, ese es")
    assert summary.reply_kind == "summary"
    assert state(world).slots.duplicate_ref == "TXN-4"
