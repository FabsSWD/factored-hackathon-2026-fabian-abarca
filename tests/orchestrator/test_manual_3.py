"""M12 manual test 3 (t1_purchase, es): the charge existed (El Buen Sabor, USD 38.50) but the
conversation ended in ESC-09. Policy 0.4.8: generic merchants are categories, periods of days,
flow_help only without details.

The customer's messages are the ones of that test; the extraction each one gets is what
extract@1.9.0 is asked to return for it.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.contracts import Confirmation, Outcome, ReasonCode, SideQuestion, TransactionRef
from tests.orchestrator.fakes import World, _txn, build_world, ext

T0 = "Hola, tengo un cargo que no reconozco en mi tarjeta"
T2 = (
    "el monto era como de 40 dólares, la compra fue en un restaurante pero no recuerdo bien "
    "su nombre"
)
T3 = "Si, fue entre el 15 y el 19 de junio"
T4 = "ya se la di, entre el 15 y el 19 de junio"

RESTAURANT = TransactionRef(merchant="restaurante", amount=Decimal("40"), amount_approximate=True)
PERIOD = TransactionRef(date_from=date(2026, 6, 15), date_to=date(2026, 6, 17))
LINE = "1. 16/06/2026 · ****4821 · Restaurante El Buen Sabor · USD 38,50"
FLOW_HELP_ES = "Sí, puedo buscar la compra con lo que recuerde"


def world_with_shops() -> World:
    world = build_world()
    world.bank.transactions += [
        _txn(
            "TXN-BS",
            merchant_name="Restaurante El Buen Sabor",
            merchant_category="Food",
            amount=Decimal("38.50"),
            amount_usd=Decimal("38.50"),
            transaction_date=datetime(2026, 6, 16, 13, 5),
        ),
        _txn(
            "TXN-SA",
            merchant_name="Super Ahorro",
            merchant_category="Food",
            amount=Decimal("120"),
            amount_usd=Decimal("120"),
            transaction_date=datetime(2026, 6, 14, 10),
        ),
        _txn(
            "TXN-CI",
            merchant_name="Cine Premium",
            merchant_category="Entertainment",
            amount=Decimal("39"),
            amount_usd=Decimal("39"),
            transaction_date=datetime(2026, 6, 16, 21),
        ),
    ]
    world.say(T0, ext(reason_code=ReasonCode.UNRECOGNIZED))
    # The model may also read "no recuerdo bien su nombre" as flow_help: the details win.
    world.say(T2, ext(transaction_ref=RESTAURANT, side=SideQuestion.FLOW_HELP))
    world.say(T3, ext(transaction_ref=PERIOD, confirmation=Confirmation.CONFIRMED))
    world.say(T4, ext(transaction_ref=PERIOD))
    return world


def test_the_manual_conversation_finds_the_restaurant_and_never_escalates() -> None:
    world = world_with_shops()
    assert world.turn(T0).reply_kind == "clarify:transaction_ref"

    restaurant = world.turn(T2)
    assert FLOW_HELP_ES not in restaurant.reply  # the details were processed instead
    assert restaurant.reply == (
        "No encontré una coincidencia exacta, pero encontré esta compra parecida. ¿Es esta? "
        f"Puede responder sí o no.\n\n{LINE}"
    )  # "restaurante" is Food: not the cinema of 39, not the Food purchase of 120

    period = world.turn(T3)
    assert LINE in period.reply and period.outcome is Outcome.CLARIFY

    again = world.turn(T4)
    assert again.outcome is Outcome.CLARIFY and not again.closed  # never ESC-09
    assert LINE in again.reply
    assert world.bank.packets == []


def test_flow_help_with_details_processes_the_details() -> None:
    world = world_with_shops()
    world.turn(T0)
    world.turn(T2)
    trace = world.tracer.traces[-1]
    assert trace.side_question is None  # no help text was sent
    stored = world.orchestrator._store.get("CONV-1")
    assert stored is not None and stored.slots.transaction_ref is not None
    assert stored.slots.transaction_ref.merchant == "restaurante"


def test_flow_help_without_details_still_helps() -> None:
    world = world_with_shops()
    world.turn(T0)
    world.say("no recuerdo el monto", ext(side=SideQuestion.FLOW_HELP))
    assert world.turn("no recuerdo el monto").reply.startswith(FLOW_HELP_ES)


def test_a_period_is_new_information_for_esc09() -> None:
    world = world_with_shops()
    world.turn(T0)
    world.turn(T2)  # listed: counted once
    world.turn(T3)  # a period: new information, the list of T2 is not counted
    stored = world.orchestrator._store.get("CONV-1")
    assert stored is not None
    assert sum(stored.counters.clarifications_by_slot.values()) == 1


def test_a_period_replaces_a_day_and_is_said_back() -> None:
    world = build_world()
    message = "Fue en la Zapatería Inventada el 10 de junio"
    day = TransactionRef(merchant="Zapatería Inventada", transaction_date=date(2026, 6, 10))
    world.say(message, ext(transaction_ref=day, reason_code=ReasonCode.UNRECOGNIZED))
    world.turn(message)
    world.say("o tal vez entre el 1 y el 5 de junio", ext(transaction_ref=TransactionRef(
        date_from=date(2026, 6, 1), date_to=date(2026, 6, 5))))  # fmt: skip
    result = world.turn("o tal vez entre el 1 y el 5 de junio")
    assert result.reply.startswith(
        "Busqué entre sus compras recientes alguna en Zapatería Inventada del 01/06/2026 al "
        "05/06/2026 y no encontré ninguna."
    )
    stored = world.orchestrator._store.get("CONV-1")
    assert stored is not None and stored.slots.transaction_ref is not None
    assert stored.slots.transaction_ref.transaction_date is None
    world.say("no, fue el 3 de junio", ext(transaction_ref=TransactionRef(
        transaction_date=date(2026, 6, 3))))  # fmt: skip
    world.turn("no, fue el 3 de junio")
    stored = world.orchestrator._store.get("CONV-1")
    assert stored is not None and stored.slots.transaction_ref is not None
    assert stored.slots.transaction_ref.date_from is None


def test_a_period_in_portuguese_is_said_back() -> None:
    world = build_world()
    message = "Foi na Sapataria Inventada entre 10 e 12 de junho"
    ref = TransactionRef(
        merchant="Sapataria Inventada", date_from=date(2026, 6, 1), date_to=date(2026, 6, 3)
    )
    world.say(message, ext("pt", transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED))
    result = world.turn(message)
    assert "entre 01/06/2026 e 03/06/2026" in result.reply
