"""Declined-summary corrections, pinned with the recorded answer of the real model."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from app.contracts import (
    ClarifyTarget,
    Confirmation,
    Outcome,
    ReasonCode,
    Slots,
    TransactionRef,
)
from app.llm_adapter.adapter import parse_extraction
from app.orchestrator.corrections import apply_declined_correction
from tests.policy.conftest import evaluate, request, txn

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "llm" / "es_confirm_declined_amount.json"
)


def recorded_slots() -> Slots:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return parse_extraction(data["raw"], date(2026, 6, 17))[0].slots


def test_fixture_is_the_case_found() -> None:
    proposed = recorded_slots()
    assert proposed.confirmation is Confirmation.DECLINED
    assert proposed.reason_code is ReasonCode.INCORRECT_AMOUNT
    assert proposed.expected_amount == Decimal("40")


def test_a_different_reason_is_asked_never_replaced() -> None:
    established = Slots(
        transaction_ref=TransactionRef(transaction_id="TXN-1"),
        reason_code=ReasonCode.UNRECOGNIZED,
        card_in_possession=True,
        shared_credentials=False,
        confirmation=Confirmation.DECLINED,
    )
    correction = apply_declined_correction(established, recorded_slots())
    assert correction.clarify_target is ClarifyTarget.REASON_CODE
    assert correction.slots.reason_code is ReasonCode.UNRECOGNIZED  # unchanged
    assert correction.slots.expected_amount is None  # nothing else applied
    assert correction.slots.confirmation is None
    assert not correction.rematch


def test_an_amount_without_a_reason_change_corrects_the_transaction() -> None:
    identified = txn(
        "TXN-1", amount="50", merchant="Cafe Sintetico", when=datetime(2026, 6, 10, 14)
    )
    established = Slots(
        transaction_ref=TransactionRef(transaction_id="TXN-1"),
        reason_code=ReasonCode.INCORRECT_AMOUNT,
        expected_amount=Decimal("30"),
        confirmation=Confirmation.DECLINED,
    )
    correction = apply_declined_correction(established, recorded_slots(), identified)
    assert correction.clarify_target is None
    assert correction.rematch
    ref = correction.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_id is None  # GATE-05 matches again
    assert ref.amount == Decimal("40")
    assert (ref.transaction_date, ref.merchant) == (date(2026, 6, 10), "Cafe Sintetico")
    assert correction.slots.expected_amount == Decimal("30")  # not the corrected amount
    assert correction.slots.confirmation is None


def test_gate05_runs_again_on_the_corrected_reference() -> None:
    wrong = txn("TXN-1", amount="50", when=datetime(2026, 6, 10, 14))
    right = txn("TXN-2", amount="40", when=datetime(2026, 6, 10, 18))
    established = Slots(
        transaction_ref=TransactionRef(transaction_id="TXN-1"),
        reason_code=ReasonCode.INCORRECT_AMOUNT,
        expected_amount=Decimal("30"),
        confirmation=Confirmation.DECLINED,
    )
    correction = apply_declined_correction(established, recorded_slots(), wrong)
    decision = evaluate(request(slots=correction.slots, transaction_candidates=[wrong, right]))
    assert decision.transaction_id == "TXN-2"
    assert decision.outcome is Outcome.CLARIFY  # the summary is shown again
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


def test_other_slots_are_applied_as_candidates() -> None:
    established = Slots(
        transaction_ref=TransactionRef(transaction_date=date(2026, 6, 10), merchant="Cafe"),
        reason_code=ReasonCode.NOT_RECEIVED,
        expected_delivery_date=date(2026, 6, 12),
        merchant_contacted=False,
        confirmation=Confirmation.DECLINED,
    )
    proposed = Slots(
        reason_code=ReasonCode.NOT_RECEIVED,
        merchant_contacted=True,
        transaction_ref=TransactionRef(merchant="Cafe Sintetico"),
        confirmation=Confirmation.DECLINED,
    )
    correction = apply_declined_correction(established, proposed)
    assert correction.slots.merchant_contacted is True
    assert correction.slots.expected_delivery_date == date(2026, 6, 12)
    assert correction.rematch
    ref = correction.slots.transaction_ref
    assert ref is not None and ref.merchant == "Cafe Sintetico"
    assert ref.transaction_date == date(2026, 6, 10)


def test_a_reply_without_new_slots_only_clears_the_confirmation() -> None:
    established = Slots(reason_code=ReasonCode.FEE, confirmation=Confirmation.DECLINED)
    correction = apply_declined_correction(established, Slots(confirmation=Confirmation.DECLINED))
    assert correction.slots == established.model_copy(update={"confirmation": None})
    assert not correction.rematch and correction.clarify_target is None


def test_amount_given_as_a_transaction_reference_wins() -> None:
    established = Slots(
        reason_code=ReasonCode.DUPLICATE, transaction_ref=TransactionRef(amount=Decimal("10"))
    )
    proposed = Slots(
        transaction_ref=TransactionRef(amount=Decimal("12")), expected_amount=Decimal("99")
    )
    ref = apply_declined_correction(established, proposed).slots.transaction_ref
    assert ref is not None and ref.amount == Decimal("12")


def test_a_new_date_rematches_without_an_established_reference() -> None:
    fee = txn("TXN-F", transaction_type="Adjustment", merchant=None, when=datetime(2026, 6, 9, 9))
    proposed = Slots(transaction_ref=TransactionRef(transaction_date=date(2026, 6, 11)))
    correction = apply_declined_correction(Slots(reason_code=ReasonCode.FEE), proposed, fee)
    ref = correction.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_date == date(2026, 6, 11)
    assert ref.merchant is None  # the identified fee has no merchant
    assert correction.rematch
