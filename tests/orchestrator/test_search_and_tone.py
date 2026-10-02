"""Findings of the M12 manual test 2 (t1_purchase, es): the bot sounded like a form and
escalated on its own failure. Contracts 18 to 21 and policy 0.4.7.

The customer's messages are the ones of that test; the extraction each one gets is what
extract@1.8.0 is asked to return for it.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

from app.contracts import (
    ClarifyTarget,
    Confirmation,
    Outcome,
    ReasonCode,
    SideQuestion,
    TransactionRef,
)
from tests.orchestrator.fakes import REF_CAFE, World, _txn, build_world, ext

T0 = "Hola, tengo un cargo no reconocido en mi tarjeta a horas de la tarde"
T1 = "Solo sé el nombre del lugar, si te lo doy me podrías confirmar lo demás?"
T2 = "la transacción del restaurante el buen sabor, fue como de 40 dólares"
T3 = "Te la acabo de decir…"

SAID = TransactionRef(
    merchant="restaurante el buen sabor", amount=Decimal("40"), amount_approximate=True
)
BUEN_SABOR_LINE = "1. 16/06/2026 · ****4821 · Restaurante El Buen Sabor · USD 38,50"


def with_buen_sabor() -> World:
    world = build_world()
    world.bank.transactions.append(
        _txn(
            "TXN-BS",
            merchant_name="Restaurante El Buen Sabor",
            amount=Decimal("38.50"),
            amount_usd=Decimal("38.50"),
            transaction_date=datetime(2026, 6, 16, 15, 10),
        )
    )
    world.say(T0, ext(reason_code=ReasonCode.UNRECOGNIZED))
    world.say(T1, ext(side=SideQuestion.FLOW_HELP))
    world.say(T2, ext(transaction_ref=SAID))
    world.say(T3, ext())
    return world


def state(world: World):  # type: ignore[no-untyped-def]
    return world.orchestrator._store.get("CONV-1")


# --- 6. The manual conversation, end to end ------------------------------------------------------


def test_the_manual_conversation_finds_the_charge_and_never_escalates() -> None:
    world = with_buen_sabor()
    first = world.turn(T0)
    assert first.reply_kind == "clarify:transaction_ref"

    help_ = world.turn(T1)
    assert help_.reply == (
        "Sí, puedo buscar la compra con lo que recuerde: el nombre del comercio, la fecha "
        "aproximada o el monto. Dígamelo y le muestro lo que encuentre."
    )  # flow_help, not "only disputes" and not the same question again

    listed = world.turn(T2)
    assert listed.reply == (
        "No encontré una coincidencia exacta, pero encontré esta compra parecida. ¿Es esta? "
        f"Puede responder sí o no.\n\n{BUEN_SABOR_LINE}"
    )

    again = world.turn(T3)
    assert again.outcome is Outcome.CLARIFY and not again.closed  # no ESC-09
    assert again.reply == (
        f"¿Es esta la compra que quiere disputar? Puede responder sí o no.\n\n{BUEN_SABOR_LINE}"
    )  # the list again, with another wording
    assert world.bank.packets == []

    world.say("sí, esa es", ext(confirmation=Confirmation.CONFIRMED))
    picked = world.turn("sí, esa es")
    assert picked.reply_kind == "block_offer"  # identified: the flow goes on
    assert "Restaurante El Buen Sabor · USD 38,50" in picked.reply
    assert state(world).picked_candidate == 1


def test_no_reply_of_the_manual_conversation_uses_tu() -> None:
    world = with_buen_sabor()
    for message in (T0, T1, T2, T3):
        reply = world.turn(message).reply.lower()
        assert " te " not in f" {reply} " and "estés" not in reply and "tienes" not in reply


# --- 1. A single relaxed candidate is confirmed or declined -------------------------------------


def test_declining_the_single_candidate_asks_for_the_transaction_again() -> None:
    world = with_buen_sabor()
    for message in (T0, T2):
        world.turn(message)
    world.say("no, esa no es", ext(confirmation=Confirmation.DECLINED))
    result = world.turn("no, esa no es")
    assert result.reply_kind == "clarify:transaction_ref"
    assert state(world).slots.transaction_ref is None


def test_one_candidate_is_asked_as_a_yes_or_no_question_to_the_llm() -> None:
    world = with_buen_sabor()
    for message in (T0, T2):
        world.turn(message)
    world.say("sí", ext(confirmation=Confirmation.CONFIRMED))
    world.turn("sí")
    assert world.llm.contexts[-1].pending_slot is not None
    assert world.llm.contexts[-1].pending_slot.value == "confirmation"


# --- 2. What was searched, and the most useful detail -------------------------------------------


def test_no_match_says_what_was_searched_and_asks_for_a_missing_detail() -> None:
    world = build_world()
    message = "Fue en la Zapatería Inventada, unos 70"
    ref = TransactionRef(
        merchant="Zapatería Inventada", amount=Decimal("70"), amount_approximate=True
    )
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    result = world.turn(message)
    assert result.reply == (
        "Busqué entre sus compras recientes alguna en Zapatería Inventada por unos 70 y no "
        "encontré ninguna.\n\n"
        "¿Recuerda la fecha aproximada de la compra? Con ese dato puedo buscar de nuevo."
    )  # no currency named: the customer's number, never formatted like an amount


def test_no_match_with_every_detail_asks_for_the_merchant_as_on_the_statement() -> None:
    world = build_world()
    message = "Zapatería Inventada, 70 dólares, el 10 de junio"
    ref = TransactionRef(
        merchant="Zapatería Inventada",
        amount=Decimal("70"),
        transaction_date=datetime(2026, 6, 10).date(),
    )
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    result = world.turn(message)
    assert "del 10/06/2026" in result.reply
    assert result.reply.endswith(
        "¿Recuerda el nombre del comercio tal como aparece en su estado de cuenta? Con ese dato "
        "puedo buscar de nuevo."
    )


def test_too_many_matches_ask_for_the_detail_that_narrows_them() -> None:
    world = build_world()
    world.bank.transactions = [
        _txn(f"TXN-{i}", merchant_name=f"Tienda {i}", transaction_date=datetime(2026, 6, 10, 9 + i))
        for i in range(5)
    ]
    message = "Fue una compra de 50 dólares el 10 de junio"
    ref = TransactionRef(amount=Decimal("50"), transaction_date=datetime(2026, 6, 10).date())
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    result = world.turn(message)
    assert result.reply == (
        "Encontré muchas compras con esos datos. ¿Recuerda el nombre del comercio? Así le "
        "muestro solo las que correspondan."
    )


def test_the_same_question_twice_names_what_is_known() -> None:
    world = build_world()
    message = "Fue en la Zapatería Inventada, unos 70"
    ref = TransactionRef(
        merchant="Zapatería Inventada", amount=Decimal("70"), amount_approximate=True
    )
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    world.turn(message)
    world.say("no sé", ext())
    second = world.turn("no sé")
    assert second.reply == (
        "Tengo estos datos de la compra: Zapatería Inventada, unos 70. ¿Recuerda la fecha "
        "aproximada de la compra? Con eso puedo buscarla mejor."
    )


def test_the_second_ask_without_details_has_its_own_wording() -> None:
    world = build_world()
    world.say("Quiero disputar un cargo", ext())
    first = world.turn("Quiero disputar un cargo")
    world.say("mmm", ext())
    second = world.turn("mmm")
    assert first.reply != second.reply
    assert second.reply.startswith("Para encontrar la compra necesito al menos uno de estos datos")


def test_each_slot_has_a_second_wording() -> None:
    world = build_world()
    message = "No reconozco el cargo de 50 dólares de Cafe Sintetico del 10 de junio"
    world.say(message, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED))
    world.turn(message)  # block offer
    world.say("no", ext(confirmation=Confirmation.DECLINED))
    first = world.turn("no")
    world.say("ehh", ext())
    second = world.turn("ehh")
    assert first.reply == "¿Tiene su tarjeta con usted en este momento?"
    assert second.reply.startswith("Para proteger su cuenta necesito saberlo")


# --- 3. ESC-09 counts only turns without new information ----------------------------------------


def test_a_clarification_answered_with_new_details_is_not_counted() -> None:
    world = with_buen_sabor()
    world.turn(T0)
    asked = state(world).counters.clarifications_by_slot[ClarifyTarget.TRANSACTION_REF]
    world.turn(T2)  # new details: the question of T0 is not counted; the list is
    by_slot = state(world).counters.clarifications_by_slot
    assert by_slot[ClarifyTarget.TRANSACTION_REF] == asked
    world.turn(T3)  # nothing new: counted
    assert state(world).counters.clarifications_by_slot[ClarifyTarget.TRANSACTION_REF] == 2


def test_turns_without_information_still_reach_esc09() -> None:
    world = build_world()
    world.say("Quiero disputar un cargo", ext())
    world.say("no sé", ext())
    for message in ("Quiero disputar un cargo", "no sé", "no sé"):
        result = world.turn(message)
    assert result.outcome is Outcome.ESCALATE
    assert world.bank.packets[0].triggered_rules == ["ESC-09"]


# --- 4. flow_help and other ---------------------------------------------------------------------


def test_flow_help_about_another_question_is_generic() -> None:
    world = build_world()
    message = "No reconozco el cargo de 50 dólares de Cafe Sintetico del 10 de junio"
    world.say(message, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED))
    world.turn(message)
    world.say("no", ext(confirmation=Confirmation.DECLINED))
    world.turn("no")  # card in possession asked
    world.say("¿qué tengo que responder?", ext(side=SideQuestion.FLOW_HELP))
    result = world.turn("¿qué tengo que responder?")
    assert result.reply.startswith("Responda con lo que sepa;")
    assert result.reply_kind == "clarify:card_in_possession"


def test_the_transfer_after_other_is_offered_once() -> None:
    world = build_world()
    world.say("¿Tienen préstamos?", ext(side=SideQuestion.OTHER))
    world.say("¿Y tarjetas de crédito nuevas?", ext(side=SideQuestion.OTHER))
    first = world.turn("¿Tienen préstamos?")
    second = world.turn("¿Y tarjetas de crédito nuevas?")
    assert "puedo transferirle" in first.reply
    assert second.reply == "Por este canal solo puedo ayudarle con disputas de transacciones."
    assert second.outcome is Outcome.INFORM


# --- 5. Tone: connecting sentences only where they add something --------------------------------


def test_connect_is_full_on_the_first_turn_and_brief_while_collecting() -> None:
    world = with_buen_sabor()
    world.turn(T0)  # first turn
    world.turn(T2)  # collecting details
    assert world.llm.connect_modes == [False, True]


def test_bad_news_gets_full_connecting_sentences() -> None:
    world = build_world()
    world.say("Hola", ext())
    world.turn("Hola")
    message = "El cargo de Kiosko 24 de 20 dólares"
    ref = TransactionRef(merchant="Kiosko 24", amount=Decimal("20"))
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.NOT_RECEIVED))
    result = world.turn(message)
    assert result.outcome is Outcome.INFORM  # the charge is still pending
    assert world.llm.connect_modes == [False, False]


def test_connect_receives_the_sentences_already_sent() -> None:
    world = with_buen_sabor()
    world.llm.connect_sentence = "Entiendo su situación."
    world.turn(T0)
    world.turn(T1)
    assert world.llm.connect_previous == [[], ["Entiendo su situación."]]
    assert state(world).connect_sentences == ["Entiendo su situación.", "Entiendo su situación."]


def test_no_match_names_only_the_details_given() -> None:
    world = build_world()
    message = "Un cargo de 999 dólares del 1 de junio"  # nothing on or around that day
    ref = TransactionRef(amount=Decimal("999"), transaction_date=datetime(2026, 6, 1).date())
    world.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    numbers = world.turn(message)
    assert numbers.reply.startswith(
        "Busqué entre sus compras recientes alguna por USD 999,00 del 01/06/2026 y no"
    )  # "dólares": the amount with its currency code (COM-08)
    assert "¿Recuerda el nombre del comercio?" in numbers.reply
    other = build_world()
    ref = TransactionRef(merchant="Zapatería Inventada")
    other.say("Zapatería Inventada", ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    merchant = other.turn("Zapatería Inventada")
    assert merchant.reply.startswith(
        "Busqué entre sus compras recientes alguna en Zapatería Inventada y no encontré"
    )


# --- Feedback on the recorded run: currency, thresholds, connect@1.2.0 ---------------------------

# An amount formatted like one (1.250,00 / 40.00) not preceded by its currency code (COM-08).
UNCODED_AMOUNT = re.compile(r"(?<![A-Z]{3} )(?<![\d.,])\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?!\d)")
# A number of days other than the resolution commitment (a window or limit, COM-07).
DAYS = re.compile(r"\b(\d+)\s+d[ií]as\b")


def assert_customer_safe(reply: str) -> None:
    assert not UNCODED_AMOUNT.search(reply), reply
    for days in DAYS.findall(reply):
        assert int(days) == 10, reply  # RESOLUTION_TARGET_BUSINESS_DAYS only


def test_search_replies_show_no_uncoded_amount_and_no_window() -> None:
    replies: list[str] = []
    world = with_buen_sabor()
    replies += [world.turn(message).reply for message in (T0, T1, T2, T3)]
    for message, ref in (
        ("Zapatería Inventada, unos 70", TransactionRef(merchant="Zapatería Inventada",
                                                       amount=Decimal("70.5"),
                                                       amount_approximate=True)),
        ("unos 160000 pesos en Zapatería Inventada", TransactionRef(merchant="Zapatería Inventada",
                                                                    amount=Decimal("160000"))),
    ):  # fmt: skip
        other = build_world()
        other.say(message, ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
        replies.append(other.turn(message).reply)
        other.say("no sé", ext())
        replies.append(other.turn("no sé").reply)
    assert "unos 70,5" in replies[4]
    assert "por COP 160.000,00" in replies[6]  # "pesos" of the customer's country, Colombia
    for reply in replies:
        assert_customer_safe(reply)


def test_a_new_amount_brings_its_own_currency() -> None:
    world = build_world()
    first = TransactionRef(merchant="Zapatería Inventada", amount=Decimal("70"))
    world.say(
        "Zapatería Inventada, 70 dólares",
        ext(transaction_ref=first, reason_code=ReasonCode.UNRECOGNIZED),
    )
    world.turn("Zapatería Inventada, 70 dólares")
    world.say("no, eran 75", ext(transaction_ref=TransactionRef(amount=Decimal("75"))))
    result = world.turn("no, eran 75")
    assert result.reply.startswith(
        "Busqué entre sus compras recientes alguna en Zapatería Inventada por 75 y no"
    )  # the dollars were said about the 70, not the 75


def test_other_gets_no_connecting_sentence() -> None:
    world = build_world()
    world.llm.connect_sentence = "Con gusto le atenderemos."
    world.say("Hola", ext())
    world.turn("Hola")
    world.say("¿Puedo abrir una cuenta de ahorros?", ext(side=SideQuestion.OTHER))
    result = world.turn("¿Puedo abrir una cuenta de ahorros?")
    assert "Con gusto le atenderemos" not in result.reply
    assert len(world.llm.connect_modes) == 1  # only the first turn called connect
