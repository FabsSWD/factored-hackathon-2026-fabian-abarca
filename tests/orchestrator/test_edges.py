"""Edge paths of the Orchestrator: failing collaborators and less common replies."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.contracts import (
    CaseRecord,
    CaseStatus,
    Confirmation,
    Language,
    Outcome,
    ReasonCode,
    Tier,
    TransactionRef,
)
from app.orchestrator.replies import CompositionError, Reply
from app.templates.formatting import Locale
from app.templates.service import TemplateService
from tests.orchestrator.fakes import AS_OF, CONFIG, CUSTOMER, REF_CAFE, build_world, ext


def test_kev_exception_counts_as_unavailable() -> None:
    world = build_world()

    async def broken(*args: object, **kwargs: object) -> None:
        raise RuntimeError("kev bug")

    world.kev.signals = broken  # type: ignore[method-assign,assignment]
    world.say("Quiero disputar un cargo", ext())
    world.turn("Quiero disputar un cargo")
    assert world.tracer.traces[-1].signals.source.value == "llm_fallback"  # type: ignore[union-attr]


def test_duplicate_declined_asks_for_another_reason() -> None:
    world = build_world()
    message = "Me cobraron dos veces Streaming Plus, el segundo cargo"
    world.say(
        message,
        ext(
            transaction_ref=TransactionRef(transaction_id="TXN-5"), reason_code=ReasonCode.DUPLICATE
        ),
    )
    world.turn(message)
    world.say("no, ese no es", ext(confirmation=Confirmation.DECLINED))
    assert world.turn("no, ese no es").reply_kind == "clarify:reason_code"


def test_existing_case_is_reported_with_its_status() -> None:
    world = build_world()
    world.bank.cases.append(
        CaseRecord(
            case_id="DSP-20260601-000007",
            customer_id=CUSTOMER,
            transaction_id="TXN-1",
            reason_code=ReasonCode.FEE,
            status=CaseStatus.IN_PROCESS,
            tier=Tier.T1,
            amount=Decimal("50"),
            currency="USD",
            amount_usd=Decimal("50"),
            created_at=datetime(2026, 6, 1, tzinfo=UTC),
            business_created_at=AS_OF,
        )
    )
    message = "Quiero disputar el cargo de 50 de Cafe Sintetico del 10 de junio"
    world.say(message, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.INCORRECT_AMOUNT))
    result = world.turn(message)
    assert result.reply_kind == "inform:duplicate_case"
    assert "DSP-20260601-000007" in result.reply and "puedo transferirle" in result.reply


def test_expected_amount_is_asked_in_the_transaction_currency() -> None:
    world = build_world()
    message = "Me cobraron de más en Cafe Sintetico el 10 de junio, 50 dólares"
    world.say(message, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.INCORRECT_AMOUNT))
    result = world.turn(message)
    assert result.reply_kind == "clarify:expected_amount"
    assert "en USD" in result.reply


def test_unrecognized_transactions_are_remembered_after_resolve() -> None:
    world = build_world()
    message = "No reconozco el cargo de Cafe Sintetico del 10 de junio de 50"
    world.say(
        message,
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.UNRECOGNIZED,
            card_in_possession=True,
            shared_credentials=False,
        ),
    )
    world.turn(message)  # block offer
    world.say("no", ext(confirmation=Confirmation.DECLINED))
    world.turn("no")  # summary
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    assert world.turn("sí, confirmo").outcome is Outcome.RESOLVE
    assert world.orchestrator._store.get("CONV-1").unrecognized_ids == {"TXN-1"}  # type: ignore[union-attr]


def test_failed_transfer_after_a_failed_write_keeps_one_tool_failure() -> None:
    world = build_world()
    world.bank.case_failure = "failed"
    world.bank.transfer_failure = True
    message = "Me cobraron 50 en Cafe Sintetico el 10 de junio y acordamos 40"
    world.say(
        message,
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    world.turn(message)
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    result = world.turn("sí, confirmo")
    assert result.reply.count("No pude confirmar") == 1
    assert result.reply.endswith("puedo transferirle con un agente.")


def test_emergency_handoff_that_also_fails_offers_a_transfer() -> None:
    world = build_world()
    world.bank.crash_on = "products"
    world.bank.transfer_failure = True
    result = world.turn("Hola, quiero disputar un cargo")
    assert result.reply.startswith("No pude confirmar")
    assert result.reply.endswith("puedo transferirle con un agente.")
    assert not result.closed


def test_emergency_handoff_crash_is_recorded() -> None:
    world = build_world()
    world.bank.crash_on = "products"

    def crash(packet: object) -> None:
        raise RuntimeError("queue bug")

    from tests.orchestrator import fakes

    original = fakes.FakeTools.transfer_to_human
    fakes.FakeTools.transfer_to_human = lambda self, packet: crash(packet)  # type: ignore[method-assign,assignment,return-value]
    try:
        result = world.turn("Hola, quiero disputar un cargo")
    finally:
        fakes.FakeTools.transfer_to_human = original  # type: ignore[method-assign]
    assert result.reply.endswith("puedo transferirle con un agente.")
    assert "handoff failed" in (world.tracer.traces[-1].error or "")


def test_a_connect_failure_keeps_the_templated_text() -> None:
    world = build_world()

    async def broken(*args: object, **kwargs: object) -> str:
        raise RuntimeError("connect bug")

    world.llm.connect = broken  # type: ignore[method-assign]
    world.say("Quiero disputar un cargo", ext())
    assert "¿Qué transacción" in world.turn("Quiero disputar un cargo").reply


def test_a_trace_failure_does_not_break_the_reply() -> None:
    world = build_world()

    def broken(trace: object) -> None:
        raise RuntimeError("audit down")

    world.tracer.record = broken  # type: ignore[method-assign]
    world.say("Quiero disputar un cargo", ext())
    assert world.turn("Quiero disputar un cargo").reply_kind == "clarify:transaction_ref"


def test_composition_rules() -> None:
    templates = TemplateService.from_policy(CONFIG.parameters)
    reply = Reply(templates, Language.ES, Locale.ES_CO)
    reply.add("tool_failure")
    with pytest.raises(CompositionError, match="tool_failure"):
        reply.check(authenticated=True)
    inform = Reply(templates, Language.ES, Locale.ES_CO)
    inform.add("pending_transaction")
    with pytest.raises(CompositionError, match="offer_transfer"):
        inform.check(authenticated=True)
    early = Reply(templates, Language.ES, Locale.ES_CO)
    early.add("handoff")
    with pytest.raises(CompositionError, match="before authentication"):
        early.check(authenticated=False)
    ok = Reply(templates, Language.ES, Locale.ES_CO)
    ok.add("tool_failure")
    ok.add("handoff_unauthenticated")
    ok.check(authenticated=False)


def test_a_composition_error_becomes_the_emergency_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    world = build_world()

    def strict(self: Reply, authenticated: bool) -> None:
        if "clarify_transaction_ref" in self.ids:
            raise CompositionError("simulated")

    monkeypatch.setattr(Reply, "check", strict)
    world.say("Quiero disputar un cargo", ext())
    result = world.turn("Quiero disputar un cargo")
    assert result.reply_kind == "tool_failure"
    assert "CompositionError" in (world.tracer.traces[-1].error or "")


def test_candidate_without_a_known_product_uses_a_placeholder() -> None:
    templates = TemplateService.from_policy(CONFIG.parameters)
    reply = Reply(templates, Language.ES, Locale.ES_CO)
    from tests.orchestrator.fakes import TRANSACTIONS

    reply.candidates([TRANSACTIONS[0]], {})
    assert "****0000" in reply.text


def test_other_details_given_instead_of_answering_the_block_offer_are_kept() -> None:
    world = build_world()
    message = "No reconozco el cargo de Cafe Sintetico del 10 de junio de 50"
    world.say(message, ext(transaction_ref=REF_CAFE, reason_code=ReasonCode.UNRECOGNIZED))
    assert world.turn(message).reply_kind == "block_offer"
    world.say("la tarjeta la tengo conmigo", ext(card_in_possession=True))
    assert world.turn("la tarjeta la tengo conmigo").reply_kind == "block_offer"  # asked again
    state = world.orchestrator._store.get("CONV-1")
    assert state is not None and state.slots.card_in_possession is True


def test_candidates_of_the_same_day_show_the_time() -> None:
    world = build_world()
    message = "Quiero disputar un cobro de Streaming Plus de 18,90"
    world.say(message, ext(transaction_ref=TransactionRef(merchant="Streaming Plus")))
    reply = world.turn(message).reply
    assert "15/06/2026 20:00" in reply and "15/06/2026 08:00" in reply


def test_collected_duplicate_ref_is_not_shown_as_said_by_the_customer() -> None:
    from app.handoff.builder import DUPLICATE_CONFIRMED

    world = build_world()
    message = "Me cobraron dos veces Streaming Plus, el segundo cargo"
    world.say(
        message,
        ext(
            transaction_ref=TransactionRef(transaction_id="TXN-5"), reason_code=ReasonCode.DUPLICATE
        ),
    )
    world.turn(message)
    world.say("sí, ese es", ext(confirmation=Confirmation.CONFIRMED))
    world.turn("sí, ese es")
    world.say("quiero un agente", ext(flags={"human_requested": True}))
    world.turn("quiero un agente")
    (packet,) = world.bank.packets
    collected = {s.name.value: s.value for s in packet.collected_slots}
    assert collected["duplicate_ref"] == DUPLICATE_CONFIRMED
    assert any(f.fact.startswith("Possible duplicate:") for f in packet.verified_facts)
