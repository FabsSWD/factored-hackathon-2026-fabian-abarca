"""M12: one full flow per outcome, and the clarification order of policy §10."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from app.contracts import (
    Confirmation,
    Outcome,
    ReasonCode,
    SlotName,
    TransactionRef,
)
from tests.orchestrator.fakes import REF_CAFE, build_world, ext


def test_resolve_creates_and_verifies_the_case() -> None:
    world = build_world()
    world.say(
        "Me cobraron 50 dólares en Cafe Sintetico el 10 de junio, pero acordamos 40",
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
            claims=["Acordó pagar 40 dólares"],
        ),
    )
    first = world.turn("Me cobraron 50 dólares en Cafe Sintetico el 10 de junio, pero acordamos 40")
    assert first.reply_kind == "summary"
    assert "USD 50,00" in first.reply  # es-CO format, currency code first
    assert "****4821" in first.reply
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    second = world.turn("sí, confirmo")
    assert second.outcome is Outcome.RESOLVE
    assert second.reply_kind == "case_created"
    (case,) = world.bank.cases
    assert case.case_id in second.reply  # shown only after the read-back
    assert case.transaction_id == "TXN-1"


def test_clarify_asks_one_slot_per_turn_in_policy_order() -> None:
    world = build_world()
    world.say("Quiero disputar un cargo", ext())
    world.say(
        "Fue el de Electro Mundo", ext(transaction_ref=TransactionRef(merchant="Electro Mundo"))
    )
    world.say("Nunca me llegó", ext(reason_code=ReasonCode.NOT_RECEIVED))
    world.say("Debía llegar el 14 de junio", ext(expected_delivery_date=date(2026, 6, 14)))
    kinds = [
        world.turn(m).reply_kind
        for m in (
            "Quiero disputar un cargo",
            "Fue el de Electro Mundo",
            "Nunca me llegó",
        )
    ]
    # Electro Mundo is a T3 amount: after the reason it escalates without asking the slots.
    assert kinds == ["clarify:transaction_ref", "clarify:reason_code", "handoff"]


def test_clarify_order_for_reason_specific_slots() -> None:
    world = build_world()
    world.say("Quiero disputar un cargo", ext())
    world.say("El de Cafe Sintetico del 10 de junio", ext(transaction_ref=REF_CAFE))
    world.say("No me entregaron el pedido", ext(reason_code=ReasonCode.NOT_RECEIVED))
    world.say("Debía llegar el 14 de junio", ext(expected_delivery_date=date(2026, 6, 14)))
    world.say("Sí, ya les escribí", ext(merchant_contacted=True))
    kinds = [
        world.turn(m).reply_kind
        for m in (
            "Quiero disputar un cargo",
            "El de Cafe Sintetico del 10 de junio",
            "No me entregaron el pedido",
            "Debía llegar el 14 de junio",
            "Sí, ya les escribí",
        )
    ]
    assert kinds == [
        "clarify:transaction_ref",
        "clarify:reason_code",
        "clarify:expected_delivery_date",
        "clarify:merchant_contacted",
        "summary",
    ]


def test_inform_for_a_pending_transaction() -> None:
    world = build_world()
    world.say(
        "No reconozco un cargo de Kiosko 24 de ayer",
        ext(transaction_ref=TransactionRef(merchant="Kiosko 24"), reason_code=ReasonCode.DUPLICATE),
    )
    result = world.turn("No reconozco un cargo de Kiosko 24 de ayer")
    assert result.outcome is Outcome.INFORM
    assert result.reply_kind == "inform:transaction_pending"
    assert "pendiente" in result.reply
    assert result.reply.rstrip().endswith("puedo transferirle con un agente.")  # offer_transfer


def test_escalate_high_amount_with_handoff() -> None:
    world = build_world()
    message = "No recibí la laptop de Electro Mundo del 12 de junio"
    world.say(
        message,
        ext(
            transaction_ref=TransactionRef(
                transaction_date=date(2026, 6, 12), merchant="Electro Mundo"
            ),
            reason_code=ReasonCode.NOT_RECEIVED,
            claims=["No recibió la laptop"],
        ),
    )
    result = world.turn(message)
    assert result.outcome is Outcome.ESCALATE and result.closed
    (packet,) = world.bank.packets
    assert packet.triggered_rules == ["ESC-01"]
    assert packet.draft_case is not None and packet.draft_case.transaction_ref == "TXN-3"
    assert "agente" in result.reply
    # The tracking number is the packet the Tool Layer wrote and read back, in the text and apart.
    assert result.handoff_reference == packet.handoff_id
    assert f"Su número de seguimiento es {packet.handoff_id}." in result.reply
    world.say("¿ya me atienden?", ext())
    later = world.turn("¿ya me atienden?")
    assert later.handoff_reference is None  # said once, in the turn that escalates


def test_refuse_another_customers_record() -> None:
    world = build_world()
    world.say(
        "Quiero disputar la transacción TXN-X",
        ext(transaction_ref=TransactionRef(transaction_id="TXN-X"), reason_code=ReasonCode.FEE),
    )
    result = world.turn("Quiero disputar la transacción TXN-X")
    assert result.outcome is Outcome.REFUSE
    assert "TXN-X" not in result.reply  # neither confirmed nor denied
    assert "propios productos" in result.reply


def test_collected_slots_show_what_the_customer_said() -> None:
    # Decision 14: a real conversation ends in a packet whose collected_slots carry the
    # customer's words, never the resolved ID.
    world = build_world()
    world.say(
        "Quiero disputar un cobro de Streaming Plus",
        ext(
            transaction_ref=TransactionRef(merchant="Streaming Plus"),
            claims=["Cobro de Streaming Plus"],
        ),
    )
    listed = world.turn("Quiero disputar un cobro de Streaming Plus")
    assert listed.reply_kind == "clarify:transaction_ref"  # two candidates listed
    world.say("la segunda", ext(transaction_ref=TransactionRef(transaction_id="TXN-4")))
    world.turn("la segunda")
    world.say(
        "Quiero hablar con una persona",
        ext(flags={"human_requested": True}, claims=["Quiere hablar con una persona"]),
    )
    world.turn("Quiero hablar con una persona")
    (packet,) = world.bank.packets
    collected = {s.name: s for s in packet.collected_slots}
    assert collected[SlotName.TRANSACTION_REF].value == "candidate 2 of the list shown"
    assert "TXN-4" not in collected[SlotName.TRANSACTION_REF].value
    assert collected[SlotName.TRANSACTION_REF].turn_index == 1
    assert any(f.record_id == "TXN-4" for f in packet.verified_facts)
