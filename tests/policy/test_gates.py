"""One test per gate and branch of policy §5, with the borders the milestone names."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.contracts import (
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    InformReason,
    Outcome,
    Queue,
    ReasonCode,
    TransactionRef,
)
from tests.policy.conftest import (
    AS_OF,
    TXN_DATE,
    case,
    counters,
    customer,
    days_before_business_date,
    evaluate,
    gate,
    product,
    request,
    slots,
    txn,
    unauthenticated,
)


def test_happy_path_reaches_the_summary() -> None:
    decision = evaluate(request())
    assert [g.gate_id for g in decision.gates_evaluated] == [f"GATE-{i:02d}" for i in range(1, 12)]
    assert all(g.passed for g in decision.gates_evaluated)
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION
    assert decision.triggered_rules == []
    assert decision.transaction_id == "TXN-1"


# --- GATE-01 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["es", "pt"])
def test_gate01_supported_languages(language: str) -> None:
    assert gate(evaluate(request(detected_language=language)), "GATE-01") is True


@pytest.mark.parametrize(
    ("language", "ambiguous"), [("en", False), (None, False), ("es", True), ("pt", True)]
)
def test_gate01_asks_once_for_the_language(language: str | None, ambiguous: bool) -> None:
    decision = evaluate(unauthenticated(detected_language=language, language_ambiguous=ambiguous))
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.LANGUAGE
    assert decision.gates_evaluated[-1].gate_id == "GATE-01"


def test_gate01_then_esc12() -> None:
    decision = evaluate(
        unauthenticated(detected_language="en", counters=counters(language_clarifications=1))
    )
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-12"]
    assert decision.queue is Queue.DISPUTES


# --- GATE-02 ---------------------------------------------------------------------------------


def test_gate02_without_session_asks_to_authenticate() -> None:
    decision = evaluate(unauthenticated())
    assert gate(decision, "GATE-02") is False
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.AUTHENTICATION
    assert decision.transaction_id is None and decision.reason_code is None


def test_gate02_explicit_refusal_informs_at_once() -> None:
    req = unauthenticated(flags={"authentication_declined": True})
    decision = evaluate(req)
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is InformReason.AUTHENTICATION_DECLINED


@pytest.mark.parametrize(("attempts", "outcome"), [(3, Outcome.CLARIFY), (4, Outcome.INFORM)])
def test_gate02_attempts_exceeding_the_maximum(attempts: int, outcome: Outcome) -> None:
    decision = evaluate(unauthenticated(counters=counters(authentication_attempts=attempts)))
    assert decision.outcome is outcome
    if outcome is Outcome.INFORM:
        assert decision.inform_reason is InformReason.AUTHENTICATION_ATTEMPTS_EXCEEDED


@pytest.mark.parametrize(
    ("age_min", "idle_min", "valid"),
    [
        (59.99, 1, True),
        (60, 1, False),  # "younger than SESSION_MAX_AGE_MIN"
        (30, 14.99, True),
        (30, 15, False),  # "idle less than SESSION_IDLE_TIMEOUT_MIN"
    ],
)
def test_gate02_session_age_and_idle_borders(age_min: float, idle_min: float, valid: bool) -> None:
    from tests.policy.conftest import session

    decision = evaluate(request(session=session(age_min=age_min, idle_min=idle_min)))
    assert gate(decision, "GATE-02") is valid
    if not valid:
        assert decision.clarify_target is ClarifyTarget.AUTHENTICATION
        # Records present with an expired session are not used.
        assert decision.transaction_id is None


def test_gate02_expired_session_invalidates_a_confirmation() -> None:
    from tests.policy.conftest import session

    req = request(session=session(age_min=61), slots=slots(confirmation=Confirmation.CONFIRMED))
    decision = evaluate(req)
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.AUTHENTICATION
    assert decision.authorized_actions == []


# --- GATE-03 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["Inactive", "Closed", "Suspended"])
def test_gate03_inactive_customer_escalates(status: str) -> None:
    decision = evaluate(request(customer=customer(status)))
    assert gate(decision, "GATE-03") is False
    assert decision.triggered_rules == ["ESC-08"]


def test_gate03_missing_customer_record_escalates() -> None:
    decision = evaluate(request(customer=None))
    assert decision.triggered_rules == ["ESC-08"]
    assert "customer_record_missing" in decision.notes


# --- GATE-04 ---------------------------------------------------------------------------------


def test_gate04_ownership_violation_refuses() -> None:
    decision = evaluate(request(ownership_violation=True))
    assert decision.outcome is Outcome.REFUSE
    assert gate(decision, "GATE-04") is False
    assert decision.authorized_actions == []
    assert decision.transaction_id is None


def test_gate04_does_not_reveal_whether_the_record_exists() -> None:
    # The Tool Layer answers access_denied both for another customer's record and for a record
    # that does not exist, so the engine sees the same request and must answer the same way.
    another_customers = request(
        ownership_violation=True,
        slots=slots(transaction_ref=TransactionRef(transaction_id="TXN-X")),
    )
    nonexistent = request(
        ownership_violation=True,
        slots=slots(transaction_ref=TransactionRef(transaction_id="TXN-X")),
    )
    first, second = evaluate(another_customers), evaluate(nonexistent)
    assert first == second
    assert first.notes == [] and first.candidate_transaction_ids == []
    from tests.policy.conftest import engine

    assert not any("TXN-X" in line for line in engine().explain(first))


# --- GATE-05 ---------------------------------------------------------------------------------


def test_gate05_missing_reference() -> None:
    decision = evaluate(request(slots=slots(transaction_ref=None)))
    assert gate(decision, "GATE-05") is False
    assert decision.clarify_target is ClarifyTarget.TRANSACTION_REF
    assert decision.candidate_transaction_ids == []


def test_gate05_lists_two_to_max_candidates() -> None:
    pool = [txn("TXN-1"), txn("TXN-2", when=TXN_DATE + timedelta(hours=3))]
    ref = TransactionRef(amount=Decimal("50"))
    decision = evaluate(request(transaction_candidates=pool, slots=slots(transaction_ref=ref)))
    assert decision.clarify_target is ClarifyTarget.TRANSACTION_REF
    assert decision.candidate_transaction_ids == ["TXN-2", "TXN-1"]


@pytest.mark.parametrize(("count", "listed"), [(3, True), (4, False)])
def test_gate05_more_than_max_candidates_asks_for_details(count: int, listed: bool) -> None:
    pool = [txn(f"TXN-{i}", when=TXN_DATE + timedelta(hours=i)) for i in range(count)]
    ref = TransactionRef(amount=Decimal("50"))
    decision = evaluate(request(transaction_candidates=pool, slots=slots(transaction_ref=ref)))
    assert decision.clarify_target is ClarifyTarget.TRANSACTION_REF
    assert bool(decision.candidate_transaction_ids) is listed
    assert ("too_many_matches" in decision.notes) is not listed


def test_gate05_no_match() -> None:
    ref = TransactionRef(amount=Decimal("999"))
    decision = evaluate(request(slots=slots(transaction_ref=ref)))
    assert decision.clarify_target is ClarifyTarget.TRANSACTION_REF
    assert "no_matching_transaction" in decision.notes


def test_gate05_fee_reference_is_asked_with_the_fee_question() -> None:
    req = request(slots=slots(transaction_ref=None, reason_code=ReasonCode.FEE))
    assert evaluate(req).clarify_target is ClarifyTarget.FEE_REF


def test_gate05_inconsistent_id_is_ambiguous() -> None:
    ref = TransactionRef(transaction_id="TXN-1", amount=Decimal("60"))
    decision = evaluate(request(slots=slots(transaction_ref=ref)))
    assert decision.clarify_target is ClarifyTarget.TRANSACTION_REF
    assert "transaction_id_inconsistent" in decision.notes


def test_gate05_unique_match_by_details() -> None:
    ref = TransactionRef(transaction_date=date(2026, 6, 10), merchant="café sintético")
    decision = evaluate(request(slots=slots(transaction_ref=ref)))
    assert gate(decision, "GATE-05") is True
    assert decision.transaction_id == "TXN-1"


# --- GATE-06 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        ("Pending", InformReason.TRANSACTION_PENDING),
        ("Declined", InformReason.TRANSACTION_DECLINED),
        ("Reversed", InformReason.TRANSACTION_REVERSED),
    ],
)
def test_gate06_status_not_approved_informs(status: str, reason: InformReason) -> None:
    decision = evaluate(request(transaction_candidates=[txn(status=status)]))
    assert gate(decision, "GATE-06") is False
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is reason


def test_gate06_unknown_status_is_a_data_contract_error() -> None:
    with pytest.raises(ValueError, match="transaction_status"):
        evaluate(request(transaction_candidates=[txn(status="Unknown")]))


def test_reason_code_is_asked_after_the_transaction() -> None:
    decision = evaluate(request(slots=slots(reason_code=None)))
    assert gate(decision, "GATE-06") is True
    assert gate(decision, "GATE-07") is None
    assert decision.clarify_target is ClarifyTarget.REASON_CODE


# --- GATE-07 (full table in test_disputability.py) -------------------------------------------


def test_gate07_unknown_type_is_a_data_contract_error() -> None:
    with pytest.raises(ValueError, match="transaction_type"):
        evaluate(request(transaction_candidates=[txn(transaction_type="Unknown")]))


# --- GATE-08 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("age", "outcome", "rule"),
    [
        (0, Outcome.CLARIFY, None),
        (60, Outcome.CLARIFY, None),
        (61, Outcome.ESCALATE, "ESC-07"),
        (120, Outcome.ESCALATE, "ESC-07"),
        (121, Outcome.INFORM, None),
    ],
)
def test_gate08_window_borders(age: int, outcome: Outcome, rule: str | None) -> None:
    decision = evaluate(request(transaction_candidates=[txn(when=days_before_business_date(age))]))
    assert gate(decision, "GATE-08") is (age <= 60)
    assert decision.outcome is outcome
    assert decision.triggered_rules == ([rule] if rule else [])
    if outcome is Outcome.INFORM:
        assert decision.inform_reason is InformReason.OUTSIDE_WINDOW


def test_gate08_age_uses_the_business_day_cutoff() -> None:
    # 05:59 on the day after belongs to the business day 60 days back.
    when = days_before_business_date(59, hour=5).replace(minute=59)
    assert gate(evaluate(request(transaction_candidates=[txn(when=when)])), "GATE-08") is True
    when = days_before_business_date(60, hour=5).replace(minute=59)  # business day 61 back
    assert gate(evaluate(request(transaction_candidates=[txn(when=when)])), "GATE-08") is False


# --- GATE-09 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(("status", "passed"), [("Active", True), ("Blocked", True)])
def test_gate09_eligible_product_statuses(status: str, passed: bool) -> None:
    assert gate(evaluate(request(products=[product(status=status)])), "GATE-09") is passed


@pytest.mark.parametrize("status", ["Closed", "Suspended"])
def test_gate09_ineligible_product_escalates(status: str) -> None:
    decision = evaluate(request(products=[product(status=status)]))
    assert gate(decision, "GATE-09") is False
    assert decision.triggered_rules == ["ESC-08"]


def test_gate09_missing_product_escalates() -> None:
    decision = evaluate(request(products=[product(product_id="PRD-OTHER")]))
    assert decision.triggered_rules == ["ESC-08"]
    assert "product_record_missing" in decision.notes


# --- GATE-10 ---------------------------------------------------------------------------------


def unrecognized(**values: object):  # type: ignore[no-untyped-def]
    return request(slots=slots(reason_code=ReasonCode.UNRECOGNIZED, **values))


def test_gate10_unrecognized_asks_card_then_credentials() -> None:
    assert evaluate(unrecognized()).clarify_target is ClarifyTarget.CARD_IN_POSSESSION
    decision = evaluate(unrecognized(card_in_possession=True))
    assert decision.clarify_target is ClarifyTarget.SHARED_CREDENTIALS


@pytest.mark.parametrize("in_possession", [True, False])
def test_gate10_unrecognized_passes_with_or_without_the_card(in_possession: bool) -> None:
    decision = evaluate(unrecognized(card_in_possession=in_possession, shared_credentials=False))
    assert gate(decision, "GATE-10") is True
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


def test_gate10_shared_credentials_escalate_esc03() -> None:
    decision = evaluate(unrecognized(shared_credentials=True))
    assert gate(decision, "GATE-10") is False
    assert decision.triggered_rules == ["ESC-03"]
    assert decision.queue is Queue.FRAUD


@pytest.mark.parametrize(
    ("expected", "outcome"),
    [(None, Outcome.CLARIFY), ("50", Outcome.INFORM), ("60", Outcome.INFORM), ("49.99", None)],
)
def test_gate10_incorrect_amount(expected: str | None, outcome: Outcome | None) -> None:
    value = Decimal(expected) if expected else None
    decision = evaluate(request(slots=slots(expected_amount=value)))
    assert gate(decision, "GATE-10") is (outcome is None)
    if outcome is Outcome.CLARIFY:
        assert decision.clarify_target is ClarifyTarget.EXPECTED_AMOUNT
    if outcome is Outcome.INFORM:
        assert decision.inform_reason is InformReason.AMOUNT_NOT_EXCEEDED


def not_received(**values: object):  # type: ignore[no-untyped-def]
    return request(slots=slots(reason_code=ReasonCode.NOT_RECEIVED, **values))


def test_gate10_not_received_asks_delivery_date() -> None:
    assert evaluate(not_received()).clarify_target is ClarifyTarget.EXPECTED_DELIVERY_DATE


def test_gate10_delivery_date_before_the_transaction_is_invalid() -> None:
    decision = evaluate(not_received(expected_delivery_date=date(2026, 6, 9)))
    assert decision.clarify_target is ClarifyTarget.EXPECTED_DELIVERY_DATE
    assert "expected_delivery_date_before_transaction" in decision.notes


@pytest.mark.parametrize("delivery", [date(2026, 6, 17), date(2026, 6, 20)])
def test_gate10_delivery_date_not_reached(delivery: date) -> None:
    decision = evaluate(not_received(expected_delivery_date=delivery, merchant_contacted=True))
    assert decision.inform_reason is InformReason.DELIVERY_DATE_NOT_REACHED


def test_gate10_not_received_merchant_contact() -> None:
    past = date(2026, 6, 16)
    asked = evaluate(not_received(expected_delivery_date=past))
    assert asked.clarify_target is ClarifyTarget.MERCHANT_CONTACTED
    no = evaluate(not_received(expected_delivery_date=past, merchant_contacted=False))
    assert no.inform_reason is InformReason.MERCHANT_NOT_CONTACTED
    yes = evaluate(not_received(expected_delivery_date=past, merchant_contacted=True))
    assert gate(yes, "GATE-10") is True


def test_gate10_delivery_on_the_transaction_date_is_valid() -> None:
    decision = evaluate(
        not_received(expected_delivery_date=TXN_DATE.date(), merchant_contacted=True)
    )
    assert gate(decision, "GATE-10") is True


def test_gate10_fee_on_an_adjustment_passes() -> None:
    fee = txn(transaction_type="Adjustment", merchant=None, product_id="PRD-L")
    req = request(
        transaction_candidates=[fee],
        products=[product("PRD-L", product_type="Préstamo Personal")],
        slots=slots(reason_code=ReasonCode.FEE),
    )
    decision = evaluate(req)
    assert gate(decision, "GATE-10") is True
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


# --- GATE-10 RC_DUPLICATE --------------------------------------------------------------------


FIRST = txn("TXN-1", when=TXN_DATE)
SECOND = txn("TXN-2", when=TXN_DATE + timedelta(hours=3))


def duplicate(ref: str = "TXN-2", **values: object):  # type: ignore[no-untyped-def]
    return request(
        transaction_candidates=[FIRST, SECOND],
        slots=slots(
            reason_code=ReasonCode.DUPLICATE,
            transaction_ref=TransactionRef(transaction_id=ref),
            **values,
        ),
    )


def test_duplicate_found_asks_the_customer_to_confirm_it() -> None:
    decision = evaluate(duplicate())
    assert decision.clarify_target is ClarifyTarget.DUPLICATE_REF
    assert decision.duplicate_transaction_id == "TXN-1"
    assert decision.transaction_id == "TXN-2"


def test_duplicate_confirmed_passes_and_disputes_the_later_charge() -> None:
    decision = evaluate(duplicate(duplicate_ref="TXN-1"))
    assert gate(decision, "GATE-10") is True
    assert decision.transaction_id == "TXN-2"
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


def test_duplicate_named_original_disputes_the_later_twin() -> None:
    asked = evaluate(duplicate(ref="TXN-1"))
    assert asked.duplicate_transaction_id == "TXN-2"
    assert asked.transaction_id == "TXN-2"
    confirmed = evaluate(duplicate(ref="TXN-1", duplicate_ref="TXN-2"))
    assert confirmed.transaction_id == "TXN-2"
    assert gate(confirmed, "GATE-10") is True


def test_duplicate_ref_that_is_not_a_twin_is_asked_again() -> None:
    decision = evaluate(duplicate(duplicate_ref="TXN-9"))
    assert decision.clarify_target is ClarifyTarget.DUPLICATE_REF
    assert "duplicate_ref_not_a_twin" in decision.notes


def test_duplicate_hedged_reply_asks_again() -> None:
    decision = evaluate(duplicate(confirmation=Confirmation.HEDGED))
    assert decision.clarify_target is ClarifyTarget.DUPLICATE_REF


def test_duplicate_declined_asks_once_for_another_reason() -> None:
    decision = evaluate(duplicate(confirmation=Confirmation.DECLINED))
    assert decision.clarify_target is ClarifyTarget.REASON_CODE
    assert "duplicate_declined" in decision.notes
    req = duplicate(confirmation=Confirmation.DECLINED).model_copy(
        update={"counters": counters(clarifications_by_slot={ClarifyTarget.REASON_CODE: 1})}
    )
    escalated = evaluate(req)
    assert escalated.triggered_rules == ["ESC-09"]


def test_duplicate_withdrawn_ends_without_case() -> None:
    decision = evaluate(duplicate(confirmation=Confirmation.WITHDRAWN))
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is InformReason.DISPUTE_WITHDRAWN


def test_duplicate_not_found_asks_for_another_reason_then_escalates() -> None:
    req = request(slots=slots(reason_code=ReasonCode.DUPLICATE))
    decision = evaluate(req)
    assert decision.clarify_target is ClarifyTarget.REASON_CODE
    assert "duplicate_not_found" in decision.notes
    again = req.model_copy(
        update={"counters": counters(clarifications_by_slot={ClarifyTarget.REASON_CODE: 1})}
    )
    assert evaluate(again).triggered_rules == ["ESC-09"]


# --- GATE-11 ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "status",
    [s for s in CaseStatus if s is not CaseStatus.DRAFT],
)
def test_gate11_any_non_draft_case_blocks(status: CaseStatus) -> None:
    existing = case("CASE-9", transaction_id="TXN-1", status=status)
    decision = evaluate(request(cases=[existing]))
    assert gate(decision, "GATE-11") is False
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is InformReason.DUPLICATE_CASE
    assert decision.existing_case_id == "CASE-9"
    assert decision.existing_case_status is status


def test_gate11_draft_cases_never_count() -> None:
    draft = case("CASE-D", transaction_id="TXN-1", status=CaseStatus.DRAFT)
    assert gate(evaluate(request(cases=[draft])), "GATE-11") is True


def test_gate11_reports_the_latest_case() -> None:
    older = case("CASE-1", transaction_id="TXN-1", business_created_at=AS_OF - timedelta(days=9))
    newer = case(
        "CASE-2",
        transaction_id="TXN-1",
        status=CaseStatus.REJECTED,
        business_created_at=AS_OF - timedelta(days=2),
    )
    decision = evaluate(request(cases=[newer, older]))
    assert decision.existing_case_id == "CASE-2"
    assert decision.existing_case_status is CaseStatus.REJECTED


def test_gate11_checks_the_disputed_charge_of_a_duplicate_pair() -> None:
    existing = case("CASE-9", transaction_id="TXN-2")
    req = duplicate(ref="TXN-1", duplicate_ref="TXN-2").model_copy(update={"cases": [existing]})
    assert evaluate(req).existing_case_id == "CASE-9"
