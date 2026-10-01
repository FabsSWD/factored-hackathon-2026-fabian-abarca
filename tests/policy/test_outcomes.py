"""Precedence (§9), the COM-03 confirmation (§8), and the card block offer (ACT-03)."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.contracts import (
    ActionId,
    ClarifyTarget,
    Confirmation,
    InformReason,
    Outcome,
    ReasonCode,
    TransactionRef,
)
from tests.policy.conftest import (
    TXN_DATE,
    counters,
    evaluate,
    product,
    request,
    slots,
    txn,
    unauthenticated,
)

# --- §9 precedence ---------------------------------------------------------------------------


def test_policy_example_human_beats_missing_slot() -> None:
    decision = evaluate(request(slots=slots(expected_amount=None), flags={"human_requested": True}))
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-05"]
    assert decision.clarify_target is None


def test_policy_example_lawyer_beats_pending() -> None:
    req = request(
        transaction_candidates=[txn(status="Pending")], flags={"legal_or_vulnerability": True}
    )
    decision = evaluate(req)
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-06"]
    assert decision.inform_reason is None


def test_policy_example_other_account_and_human_is_refuse() -> None:
    decision = evaluate(request(ownership_violation=True, flags={"human_requested": True}))
    assert decision.outcome is Outcome.REFUSE
    assert decision.queue is None and decision.authorized_actions == []
    assert decision.triggered_rules == ["ESC-05"]  # recorded; REFUSE wins


@pytest.mark.parametrize(
    ("overrides", "outcome"),
    [
        # ESCALATE beats INFORM from GATE-07 (N).
        (
            {
                "transaction_candidates": [txn(transaction_type="Deposit")],
                "flags": {"human_requested": True},
            },
            Outcome.ESCALATE,
        ),
        # INFORM from a gate beats the CLARIFY that a later slot would need.
        (
            {
                "transaction_candidates": [txn(status="Reversed")],
                "slots": slots(expected_amount=None),
            },
            Outcome.INFORM,
        ),
        # Injection threshold beats a pending transaction.
        (
            {
                "transaction_candidates": [txn(status="Pending")],
                "counters": counters(injection_strikes=2),
            },
            Outcome.ESCALATE,
        ),
        # REFUSE beats ESC-13.
        (
            {"ownership_violation": True, "counters": counters(injection_strikes=2)},
            Outcome.REFUSE,
        ),
        # A record trigger beats a confirmed summary.
        (
            {
                "transaction_candidates": [txn(fraud_score=50)],
                "slots": slots(confirmation=Confirmation.CONFIRMED),
            },
            Outcome.ESCALATE,
        ),
        # An interrupt beats the authentication request.
        (
            {
                "session": None,
                "customer": None,
                "transaction_candidates": [],
                "products": [],
                "flags": {"legal_or_vulnerability": True},
            },
            Outcome.ESCALATE,
        ),
    ],
)
def test_cross_precedence(overrides: dict[str, object], outcome: Outcome) -> None:
    assert evaluate(request(**overrides)).outcome is outcome


def test_gates_stop_at_the_first_failure() -> None:
    # GATE-03 fails: the ownership violation (GATE-04) is never evaluated.
    from tests.policy.conftest import customer

    decision = evaluate(request(customer=customer("Inactive"), ownership_violation=True))
    assert decision.outcome is Outcome.ESCALATE
    assert [g.gate_id for g in decision.gates_evaluated][-1] == "GATE-03"


# --- COM-03 confirmation ---------------------------------------------------------------------


def test_summary_is_presented_before_any_write() -> None:
    decision = evaluate(request())
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION
    assert ActionId.CREATE_CASE not in decision.authorized_actions


def test_confirmed_authorizes_case_and_flag() -> None:
    decision = evaluate(request(slots=slots(confirmation=Confirmation.CONFIRMED)))
    assert decision.outcome is Outcome.RESOLVE
    assert decision.authorized_actions == [ActionId.CREATE_CASE, ActionId.RECORD_CREDIT_FLAG]
    assert decision.transaction_id == "TXN-1"
    assert decision.reason_code is ReasonCode.INCORRECT_AMOUNT


def test_hedged_asks_once_more() -> None:
    decision = evaluate(request(slots=slots(confirmation=Confirmation.HEDGED)))
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


def test_hedged_twice_escalates() -> None:
    req = request(
        slots=slots(confirmation=Confirmation.HEDGED),
        counters=counters(clarifications_by_slot={ClarifyTarget.CONFIRMATION: 2}),
    )
    assert evaluate(req).triggered_rules == ["ESC-09"]


def test_declined_asks_which_detail_is_wrong() -> None:
    decision = evaluate(request(slots=slots(confirmation=Confirmation.DECLINED)))
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.CORRECTION


def test_declined_repeatedly_escalates() -> None:
    req = request(
        slots=slots(confirmation=Confirmation.DECLINED),
        counters=counters(clarifications_by_slot={ClarifyTarget.CORRECTION: 2}),
    )
    assert evaluate(req).triggered_rules == ["ESC-09"]


def test_withdrawn_ends_without_case() -> None:
    decision = evaluate(request(slots=slots(confirmation=Confirmation.WITHDRAWN)))
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is InformReason.DISPUTE_WITHDRAWN
    assert decision.authorized_actions == []


# --- ACT-03 card block offer -----------------------------------------------------------------


def unrecognized(**overrides: object):  # type: ignore[no-untyped-def]
    base_slots = slots(reason_code=ReasonCode.UNRECOGNIZED)
    return request(**{"slots": base_slots, **overrides})


def test_card_block_offered_for_unrecognized_card_transaction() -> None:
    decision = evaluate(unrecognized())
    assert decision.clarify_target is ClarifyTarget.CARD_IN_POSSESSION
    assert decision.authorized_actions == [ActionId.BLOCK_CARD]
    assert decision.card_product_id == "PRD-1"


def test_card_block_with_lost_card_and_case_together() -> None:
    req = unrecognized(
        slots=slots(
            reason_code=ReasonCode.UNRECOGNIZED,
            card_in_possession=False,
            shared_credentials=False,
            confirmation=Confirmation.CONFIRMED,
        )
    )
    decision = evaluate(req)
    assert decision.outcome is Outcome.RESOLVE
    assert decision.authorized_actions == [
        ActionId.BLOCK_CARD,
        ActionId.CREATE_CASE,
        ActionId.RECORD_CREDIT_FLAG,
    ]


@pytest.mark.parametrize("product_type", ["Tarjeta Crédito", "Tarjeta Débito"])
def test_card_block_for_both_card_types(product_type: str) -> None:
    decision = evaluate(unrecognized(products=[product(product_type=product_type)]))
    assert decision.card_product_id == "PRD-1"


def test_no_card_block_on_an_account() -> None:
    decision = evaluate(unrecognized(products=[product(product_type="Cuenta Corriente")]))
    assert ActionId.BLOCK_CARD not in decision.authorized_actions
    assert "no_card_to_block" in decision.notes


def test_card_already_blocked_is_reported_without_offer() -> None:
    decision = evaluate(unrecognized(products=[product(status="Blocked")]))
    assert decision.card_already_blocked is True
    assert decision.card_product_id is None


def test_no_card_block_on_closed_card_and_gate09_escalates() -> None:
    decision = evaluate(unrecognized(products=[product(status="Closed")]))
    assert decision.triggered_rules == ["ESC-08"]
    assert decision.card_product_id is None and not decision.card_already_blocked


def test_card_block_offered_with_an_inform_outcome() -> None:
    decision = evaluate(unrecognized(transaction_candidates=[txn(status="Pending")]))
    assert decision.outcome is Outcome.INFORM
    assert decision.authorized_actions == [ActionId.BLOCK_CARD]


def test_unrecognized_transfer_has_no_card_block() -> None:
    req = unrecognized(
        transaction_candidates=[txn(transaction_type="Transfer", product_id="PRD-A")],
        products=[product("PRD-A", product_type="Cuenta Ahorro")],
    )
    decision = evaluate(req)
    assert decision.triggered_rules == ["ESC-14"]
    assert decision.authorized_actions == [ActionId.TRANSFER_TO_HUMAN]
    assert "no_card_to_block" in decision.notes


def test_no_card_block_before_the_transaction_is_identified() -> None:
    decision = evaluate(
        unrecognized(slots=slots(reason_code=ReasonCode.UNRECOGNIZED, transaction_ref=None))
    )
    assert decision.authorized_actions == []


def takeover(**overrides: object):  # type: ignore[no-untyped-def]
    values = {"flags": {"account_takeover_reported": True}, "slots": slots(transaction_ref=None)}
    return request(**{**values, **overrides})


def test_esc03_without_transaction_blocks_the_only_active_card() -> None:
    products = [
        product("PRD-1"),
        product("PRD-2", status="Blocked"),
        product("PRD-3", product_type="Seguro"),
    ]
    decision = evaluate(takeover(products=products))
    assert decision.outcome is Outcome.ESCALATE
    assert decision.authorized_actions == [ActionId.BLOCK_CARD, ActionId.TRANSFER_TO_HUMAN]
    assert decision.card_product_id == "PRD-1"


def test_esc03_without_transaction_and_several_cards_offers_nothing() -> None:
    products = [product("PRD-1"), product("PRD-2", product_type="Tarjeta Débito", last4="1111")]
    decision = evaluate(takeover(products=products))
    assert decision.card_product_id is None
    assert "card_block_not_offered_several_cards" in decision.notes


def test_esc03_without_transaction_and_no_active_card_offers_nothing() -> None:
    decision = evaluate(takeover(products=[product(product_type="Cuenta Ahorro")]))
    assert decision.card_product_id is None
    assert "no_active_card" in decision.notes


def test_esc03_with_identified_transaction_uses_its_card() -> None:
    products = [product("PRD-1"), product("PRD-2", last4="2222")]
    decision = evaluate(takeover(products=products, slots=slots()))
    assert decision.card_product_id == "PRD-1"


def test_no_card_block_before_authentication() -> None:
    decision = evaluate(unauthenticated(flags={"account_takeover_reported": True}))
    assert decision.card_product_id is None


def test_no_card_block_with_refuse() -> None:
    decision = evaluate(takeover(ownership_violation=True))
    assert decision.outcome is Outcome.REFUSE
    assert decision.authorized_actions == [] and decision.card_product_id is None


def test_card_block_follows_the_transaction_of_a_pick() -> None:
    pool = [txn("TXN-1"), txn("TXN-2", when=TXN_DATE + timedelta(hours=1), product_id="PRD-2")]
    req = unrecognized(
        transaction_candidates=pool,
        products=[product("PRD-1"), product("PRD-2", last4="2222")],
        slots=slots(
            reason_code=ReasonCode.UNRECOGNIZED,
            transaction_ref=TransactionRef(transaction_id="TXN-2"),
        ),
    )
    assert evaluate(req).card_product_id == "PRD-2"
