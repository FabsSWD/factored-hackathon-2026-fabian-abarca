"""M13: every gate of the policy (§5) at least once, as a whole conversation through the API.

The OpenAI double returns, for each message, what the extraction prompt asks for it; everything
else is the real application on the database (tests/e2e/conftest.py).
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from app.contracts import Confirmation, ReasonCode, TransactionRef
from tests.e2e.conftest import E2E, SUSPENDED_DOCUMENT, Conversation, Turn
from tests.orchestrator.fakes import ext
from tests.tools.conftest import add_product, add_transaction

CAFE = TransactionRef(
    merchant="Cafe Sintetico", amount=Decimal("50"), transaction_date=date(2026, 6, 16)
)


def by_id(transaction_id: str) -> TransactionRef:
    """A transaction the customer identifies by its reference (as typed or picked)."""
    return TransactionRef(transaction_id=transaction_id)


def incorrect_amount(chat: Conversation, ref: TransactionRef, expected: str) -> Turn:
    return chat.send(
        f"Me cobraron de más, había acordado {expected}",
        ext(
            transaction_ref=ref,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal(expected),
        ),
    )


# --- RESOLVE: the whole automated path -----------------------------------------------------------


def test_resolve_with_block_offer_summary_and_a_verified_case(e2e: E2E) -> None:
    chat = e2e.conversation()
    offer = chat.send(
        "No reconozco 50 dólares de Cafe Sintetico del 16 de junio; "
        "tengo mi tarjeta y no di mis claves",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.UNRECOGNIZED,
            card_in_possession=True,
            shared_credentials=False,
        ),
    )
    assert offer.outcome == "CLARIFY" and offer.trace is not None
    assert offer.trace.reply_kind == "block_offer"
    assert offer.reply.startswith("Encontré este cargo: 16/06/2026 · Cafe Sintetico · USD 50,00.")
    blocked = chat.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert blocked.reply.startswith("Bloqueamos temporalmente su tarjeta ****4821.")
    assert blocked.trace is not None and blocked.trace.reply_kind == "summary"
    done = chat.send("sí, confirmo los datos", ext(confirmation=Confirmation.CONFIRMED))
    assert done.outcome == "RESOLVE"
    (case,) = e2e.cases()
    assert case.transaction_id == "TRX-T1-PURCHASE" and case.tier == "T1"
    assert case.case_id in done.reply


# --- GATE-01 language ----------------------------------------------------------------------------


def test_gate01_an_unsupported_language_is_asked_once_then_escalates(e2e: E2E) -> None:
    chat = e2e.conversation()
    first = chat.send("I don't recognize a charge on my card", ext("en"))
    assert first.outcome == "CLARIFY" and "español" in first.reply and "português" in first.reply
    second = chat.send("English please", ext("en"))
    assert second.outcome == "ESCALATE" and second.rules == ["ESC-12"]


# --- GATE-02 authentication ----------------------------------------------------------------------


def test_gate02_without_a_session_nothing_is_read_and_login_is_asked(e2e: E2E) -> None:
    chat = e2e.conversation(document=None)
    turn = chat.send("No reconozco un cargo de Cafe Sintetico", ext(transaction_ref=CAFE))
    assert turn.outcome == "CLARIFY" and "verificar su identidad" in turn.reply
    assert e2e.kev.calls == 0  # no model besides extract before the login
    assert "Cafe Sintetico" not in turn.reply


def test_gate02_an_explicit_refusal_informs_at_once(e2e: E2E) -> None:
    chat = e2e.conversation(document=None)
    turn = chat.send("No quiero identificarme", ext(flags={"authentication_declined": True}))
    assert turn.outcome == "INFORM" and "Sin verificar su identidad" in turn.reply


def test_gate02_too_many_attempts_inform(e2e: E2E) -> None:
    # Each login request without a login is an attempt; INFORM once they exceed
    # AUTH_MAX_ATTEMPTS (3): the fifth turn, after four requests.
    chat = e2e.conversation(document=None)
    for _ in range(4):
        assert chat.send("Hola, tengo un problema").outcome == "CLARIFY"
    last = chat.send("Hola, tengo un problema")
    assert last.outcome == "INFORM" and "después de varios intentos" in last.reply


def test_gate02_the_reason_given_before_the_login_is_kept(e2e: E2E) -> None:
    chat = e2e.conversation(document=None)
    chat.send("Quiero disputar un cargo que no reconozco", ext(reason_code=ReasonCode.UNRECOGNIZED))
    chat.token = e2e.login()
    after = chat.send("Ya ingresé", ext())
    assert after.trace is not None and after.trace.reply_kind == "clarify:transaction_ref"


# --- GATE-03 customer status, GATE-09 product status ---------------------------------------------


def test_gate03_an_inactive_customer_escalates(e2e: E2E) -> None:
    chat = e2e.conversation(SUSPENDED_DOCUMENT)
    turn = incorrect_amount(
        chat, TransactionRef(merchant="Cafe Sintetico", amount=Decimal("10")), "5"
    )
    assert turn.outcome == "ESCALATE" and turn.rules == ["ESC-08"]


def test_gate09_a_closed_product_escalates(e2e: E2E) -> None:
    add_product(e2e.db, "PRD-CLOSEDCARD01", "Tarjeta Crédito", "Closed")
    add_transaction(
        e2e.db,
        "TRX-CLOSED-CARD",
        datetime(2026, 6, 15, 12),
        product_id="PRD-CLOSEDCARD01",
        amount=Decimal("60"),
        amount_usd=Decimal("60"),
        merchant_name="Optica Lux",
    )
    turn = incorrect_amount(
        e2e.conversation(), TransactionRef(merchant="Optica Lux", amount=Decimal("60")), "40"
    )
    assert turn.outcome == "ESCALATE" and turn.rules == ["ESC-08"]


# --- GATE-04 ownership ---------------------------------------------------------------------------


def test_gate04_another_customers_transaction_is_refused_and_logged(e2e: E2E) -> None:
    chat = e2e.conversation()
    turn = incorrect_amount(chat, by_id("TRX-OTHER-CUSTOMER"), "40")
    assert turn.outcome == "REFUSE"
    assert "propios productos" in turn.reply
    assert any(e["requested_id"] == "TRX-OTHER-CUSTOMER" for e in e2e.security_events())


def test_gate04_a_record_that_does_not_exist_gets_the_same_answer(e2e: E2E) -> None:
    other = incorrect_amount(e2e.conversation(), by_id("TRX-OTHER-CUSTOMER"), "40")
    missing = incorrect_amount(e2e.conversation(), by_id("TRX-DOES-NOT-EXIST"), "40")
    assert other.reply == missing.reply and other.outcome == missing.outcome == "REFUSE"


# --- GATE-05 transaction identified --------------------------------------------------------------


def test_gate05_several_matches_are_listed_and_one_is_picked(e2e: E2E) -> None:
    chat = e2e.conversation()
    listed = chat.send(
        "Un cargo de Cafe Sintetico que no reconozco",
        ext(
            transaction_ref=TransactionRef(merchant="Cafe Sintetico"),
            reason_code=ReasonCode.INCORRECT_AMOUNT,
        ),
    )
    assert (
        listed.trace is not None and len(listed.trace.decisions[0].candidate_transaction_ids) == 2
    )
    assert "1. " in listed.reply and "2. " in listed.reply
    picked = chat.send("la de 50", ext(transaction_ref=by_id("TRX-T1-PURCHASE")))
    assert (
        picked.trace is not None and picked.trace.decisions[0].transaction_id == "TRX-T1-PURCHASE"
    )


def test_gate05_nothing_found_says_what_was_searched(e2e: E2E) -> None:
    chat = e2e.conversation()
    ref = TransactionRef(merchant="Zapatería Inventada", amount=Decimal("999"))
    turn = chat.send(
        "Fue en la Zapatería Inventada, 999 dólares",
        ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED),
    )
    assert turn.outcome == "CLARIFY"
    assert turn.reply.startswith(
        "Busqué entre sus compras recientes alguna en Zapatería Inventada por USD 999,00"
    )


def test_gate05_an_approximate_amount_lists_a_near_charge(e2e: E2E) -> None:
    chat = e2e.conversation()
    ref = TransactionRef(merchant="Cafe Sintetico", amount=Decimal("45"), amount_approximate=True)
    listed = chat.send(
        "Fue en Cafe Sintetico, unos 45 dólares",
        ext(
            transaction_ref=ref,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("30"),
        ),
    )
    assert listed.reply.startswith("No encontré una coincidencia exacta, pero encontré esta compra")
    picked = chat.send("sí, esa es", ext(confirmation=Confirmation.CONFIRMED))
    assert picked.trace is not None and picked.trace.reply_kind == "summary"


# --- GATE-06 transaction status ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("transaction_id", "text"),
    [
        ("TRX-PENDING", "todavía está pendiente"),
        ("TRX-DECLINED", "fue rechazada"),
        ("TRX-REVERSED", "ya fue reversada"),
    ],
)
def test_gate06_a_transaction_that_is_not_approved_informs(
    e2e: E2E, transaction_id: str, text: str
) -> None:
    turn = incorrect_amount(e2e.conversation(), by_id(transaction_id), "10")
    assert turn.outcome == "INFORM" and text in turn.reply
    assert "puedo transferirle con un agente" in turn.reply  # every INFORM offers a human


# --- GATE-07 disputable combination --------------------------------------------------------------


def test_gate07_h_escalates_by_esc14_to_fraud(e2e: E2E) -> None:
    chat = e2e.conversation()
    turn = chat.send(
        "No reconozco la transferencia de 400.000 pesos",
        ext(transaction_ref=by_id("TRX-COP-SOURCE"), reason_code=ReasonCode.UNRECOGNIZED),
    )
    assert turn.outcome == "ESCALATE" and turn.rules == ["ESC-14"]
    assert e2e.handoff_of(turn).queue.value == "fraud"


def test_gate07_n_informs(e2e: E2E) -> None:
    chat = e2e.conversation()
    turn = chat.send(
        "Ese cargo de Cafe Sintetico es una comisión del banco que no corresponde",
        ext(transaction_ref=CAFE, reason_code=ReasonCode.FEE),
    )
    assert turn.outcome == "INFORM" and "tipo de transacción" in turn.reply


# --- GATE-08 filing window -----------------------------------------------------------------------


def test_gate08_a_late_filing_escalates_and_an_older_one_informs(e2e: E2E) -> None:
    add_transaction(
        e2e.db,
        "TRX-LATE",
        datetime(2026, 3, 19, 12),
        amount=Decimal("60"),
        amount_usd=Decimal("60"),
        merchant_name="Zapateria Vieja",
    )
    add_transaction(
        e2e.db,
        "TRX-ANCIENT",
        datetime(2025, 12, 1, 12),
        amount=Decimal("60"),
        amount_usd=Decimal("60"),
        merchant_name="Zapateria Antigua",
    )
    late = incorrect_amount(
        e2e.conversation(), TransactionRef(merchant="Zapateria Vieja", amount=Decimal("60")), "40"
    )
    assert late.outcome == "ESCALATE" and late.rules == ["ESC-07"]
    ancient = incorrect_amount(e2e.conversation(), by_id("TRX-ANCIENT"), "40")
    assert ancient.outcome == "INFORM" and "supera el plazo" in ancient.reply


# --- GATE-10 reason-specific preconditions -------------------------------------------------------


def test_gate10_incorrect_amount_not_exceeded_informs(e2e: E2E) -> None:
    turn = incorrect_amount(e2e.conversation(), CAFE, "60")
    assert turn.outcome == "INFORM" and "no supera" in turn.reply


def test_gate10_not_received_before_the_delivery_date_informs(e2e: E2E) -> None:
    chat = e2e.conversation()
    turn = chat.send(
        "No me llegó lo que compré en Cafe Sintetico; debía llegar el 30 de junio",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.NOT_RECEIVED,
            expected_delivery_date=date(2026, 6, 30),
            merchant_contacted=True,
        ),
    )
    assert turn.outcome == "INFORM" and "todavía no ha pasado" in turn.reply


def test_gate10_not_received_without_contacting_the_merchant_informs(e2e: E2E) -> None:
    chat = e2e.conversation()
    turn = chat.send(
        "No me llegó lo que compré en Cafe Sintetico; debía llegar el 16 de junio",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.NOT_RECEIVED,
            expected_delivery_date=date(2026, 6, 16),
            merchant_contacted=False,
        ),
    )
    assert turn.outcome == "INFORM" and "contactar al comercio" in turn.reply


def test_gate10_a_bank_fee_resolves(e2e: E2E) -> None:
    chat = e2e.conversation()
    summary = chat.send(
        "Me cobraron una comisión del préstamo que no corresponde",
        ext(transaction_ref=by_id("TRX-FEE"), reason_code=ReasonCode.FEE),
    )
    assert summary.trace is not None and summary.trace.reply_kind == "summary"
    done = chat.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert done.outcome == "RESOLVE"
    assert e2e.cases()[0].reason_code == "RC_FEE"


def test_gate10_a_duplicate_charge_is_confirmed_and_resolves(e2e: E2E) -> None:
    chat = e2e.conversation()
    asked = chat.send(
        "Me cobraron dos veces Streaming Plus; el segundo cargo",
        ext(transaction_ref=by_id("TRX-DUP-SECOND"), reason_code=ReasonCode.DUPLICATE),
    )
    assert asked.trace is not None and asked.trace.reply_kind == "clarify:duplicate_ref"
    assert "15/06/2026" in asked.reply and "USD 18,90" in asked.reply
    summary = chat.send("sí, ese es", ext(confirmation=Confirmation.CONFIRMED))
    assert summary.trace is not None and summary.trace.reply_kind == "summary"
    done = chat.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert done.outcome == "RESOLVE"
    assert e2e.cases()[0].transaction_id == "TRX-DUP-SECOND"  # the later charge is disputed


def test_gate10_shared_credentials_escalate_by_esc03(e2e: E2E) -> None:
    chat = e2e.conversation()
    chat.send(
        "No reconozco el cargo de Cafe Sintetico; le di mi clave a alguien",
        ext(
            transaction_ref=CAFE,
            reason_code=ReasonCode.UNRECOGNIZED,
            card_in_possession=True,
            shared_credentials=True,
        ),
    )
    turn = chat.send("no, no la bloquee", ext(confirmation=Confirmation.DECLINED))
    assert turn.outcome == "ESCALATE" and turn.rules == ["ESC-03"]


# --- GATE-11 no duplicate case -------------------------------------------------------------------


def test_gate11_a_transaction_with_a_case_informs_its_reference_and_status(e2e: E2E) -> None:
    first = e2e.conversation()
    incorrect_amount(first, CAFE, "40")
    created = first.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert created.outcome == "RESOLVE"
    (case,) = e2e.cases()
    again = incorrect_amount(e2e.conversation(), CAFE, "40")
    assert again.outcome == "INFORM"
    assert case.case_id in again.reply and "abierta" in again.reply
    assert len(e2e.cases()) == 1  # never a second case
