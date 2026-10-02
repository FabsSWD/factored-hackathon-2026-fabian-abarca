"""M13: the conversation behaviors added in the M12 manual tests, end to end on the database:
side questions, the tolerant search (approximate amounts, periods, categories), the card block
offer that names the charge, the closing after a final outcome and the replies after a handoff.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from app.contracts import Confirmation, ExtractionResult, ReasonCode, SideQuestion, TransactionRef
from app.storage.models import CardBlock, HandoffPacketRow
from tests.e2e.conftest import E2E
from tests.orchestrator.fakes import ext
from tests.tools.conftest import add_transaction, count

CAFE = TransactionRef(
    merchant="Cafe Sintetico", amount=Decimal("50"), transaction_date=date(2026, 6, 16)
)
OFFER = "Para proteger su cuenta, puedo bloquear temporalmente su tarjeta ****4821."
CHARGE = "Encontré este cargo: 16/06/2026 · Cafe Sintetico · USD 50,00."


def unrecognized_cafe() -> ExtractionResult:
    return ext(transaction_ref=CAFE, reason_code=ReasonCode.UNRECOGNIZED)


# --- Side questions -----------------------------------------------------------------------------


def test_a_refund_question_at_the_block_offer_is_answered_and_the_offer_repeated(e2e: E2E) -> None:
    chat = e2e.conversation()
    chat.send("No reconozco el cargo de Cafe Sintetico", unrecognized_cafe())
    side = chat.send("Existe una posibilidad de reembolso?", ext(side=SideQuestion.REFUND))
    assert side.reply.startswith("No puedo confirmarle un reembolso.")
    assert CHARGE in side.reply and OFFER in side.reply
    assert side.trace is not None and side.trace.side_question is SideQuestion.REFUND
    assert side.outcome == "CLARIFY" and side.trace.decisions == []


def test_case_status_reads_only_the_customers_own_cases(e2e: E2E) -> None:
    first = e2e.conversation()
    first.send(
        "Cafe Sintetico me cobró 50 y eran 40",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    first.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    (case,) = e2e.cases()
    chat = e2e.conversation()
    status = chat.send("¿Cómo va mi disputa?", ext(side=SideQuestion.CASE_STATUS))
    assert status.reply.startswith(f"Su disputa {case.case_id} está abierta.")
    other = e2e.conversation("42388496")
    theirs = other.send(f"¿Cómo va la {case.case_id}?", ext(side=SideQuestion.CASE_STATUS))
    assert theirs.reply.startswith("No encontré una disputa suya con esa referencia.")


def test_other_offers_a_transfer_once_and_no_flow(e2e: E2E) -> None:
    chat = e2e.conversation()
    first = chat.send("¿Puedo abrir una cuenta de ahorros?", ext(side=SideQuestion.OTHER))
    assert first.outcome == "INFORM" and "puedo transferirle" in first.reply
    second = chat.send("¿Y un préstamo?", ext(side=SideQuestion.OTHER))
    assert second.reply == "Por este canal solo puedo ayudarle con disputas de transacciones."


# --- Tolerant search ----------------------------------------------------------------------------


def test_a_period_and_an_approximate_amount_list_the_charge(e2e: E2E) -> None:
    chat = e2e.conversation()
    ref = TransactionRef(
        merchant="Cafe Sintetico",
        amount=Decimal("45"),
        amount_approximate=True,
        date_from=date(2026, 6, 15),
        date_to=date(2026, 6, 17),
    )
    listed = chat.send(
        "Cafe Sintetico, como de 45 dólares, entre el 15 y el 19 de junio",
        ext(
            transaction_ref=ref,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("30"),
        ),
    )
    assert "Cafe Sintetico · USD 50,00" in listed.reply
    assert "USD 8,00" not in listed.reply  # the other Cafe Sintetico charge is far from 45


def test_a_generic_restaurant_is_searched_by_category(e2e: E2E) -> None:
    add_transaction(
        e2e.db,
        "TRX-BUEN-SABOR",
        datetime(2026, 6, 16, 13),
        amount=Decimal("38.50"),
        amount_usd=Decimal("38.50"),
        merchant_name="Restaurante El Buen Sabor",
        merchant_category="Food",
    )
    add_transaction(
        e2e.db,
        "TRX-SUPER",
        datetime(2026, 6, 16, 9),
        amount=Decimal("120"),
        amount_usd=Decimal("120"),
        merchant_name="Super Ahorro",
        merchant_category="Food",
    )
    chat = e2e.conversation()
    chat.send("Hola, tengo un cargo que no reconozco", ext(reason_code=ReasonCode.UNRECOGNIZED))
    ref = TransactionRef(merchant="restaurante", amount=Decimal("40"), amount_approximate=True)
    listed = chat.send(
        "el monto era como de 40 dólares, la compra fue en un restaurante "
        "pero no recuerdo su nombre",
        ext(transaction_ref=ref, side=SideQuestion.FLOW_HELP),
    )
    assert "Restaurante El Buen Sabor · USD 38,50" in listed.reply
    assert "Super Ahorro" not in listed.reply and "Sí, puedo buscar" not in listed.reply
    picked = chat.send("sí, esa es", ext(confirmation=Confirmation.CONFIRMED))
    assert (
        picked.trace is not None and picked.trace.decisions[-1].transaction_id == "TRX-BUEN-SABOR"
    )


def test_ese_no_es_at_the_block_offer_blocks_nothing(e2e: E2E) -> None:
    chat = e2e.conversation()
    chat.send("No reconozco el cargo de Cafe Sintetico", unrecognized_cafe())
    turn = chat.send("ese no es", ext(wrong_transaction=True, confirmation=Confirmation.DECLINED))
    assert turn.trace is not None and turn.trace.reply_kind == "clarify:transaction_ref"
    assert count(e2e.db, CardBlock) == 0  # no card block was written


# --- After a final outcome ----------------------------------------------------------------------


def test_thanks_after_resolve_gets_the_closing(e2e: E2E) -> None:
    chat = e2e.conversation()
    chat.send(
        "Cafe Sintetico me cobró 50 y eran 40",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    assert chat.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED)).outcome == "RESOLVE"
    closing = chat.send("gracias", ext())
    assert closing.reply == "Con gusto. ¿Hay algo más en lo que pueda ayudarle?"
    assert closing.outcome == "RESOLVE"  # the conversation keeps its result


def test_after_a_handoff_the_customer_gets_the_notice_and_the_agent_the_message(e2e: E2E) -> None:
    chat = e2e.conversation()
    handoff = chat.send("Quiero hablar con un agente", ext(flags={"human_requested": True}))
    assert handoff.reply.startswith("Voy a transferir su caso a un agente")
    calls = e2e.llm.calls, e2e.kev.calls
    later = chat.send("Mi número es 3001234567, llámenme")
    assert later.reply.startswith("Su conversación ya fue transferida a un agente")
    assert "Voy a transferir" not in later.reply
    assert (e2e.llm.calls, e2e.kev.calls) == calls  # no model
    (row,) = e2e.handoffs()
    assert isinstance(row, HandoffPacketRow)
    (message,) = row.packet["post_handoff_messages"]
    assert "3001234567" not in message["text"] and "[number]" in message["text"]
