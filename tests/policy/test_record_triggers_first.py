"""Record-dependent triggers (ESC-01, ESC-02, ESC-04) before the reason-specific slots (§5).

Questions that cannot change the outcome would only spend turns and clarification limits."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.contracts import (
    CaseStatus,
    ClarifyTarget,
    InformReason,
    Language,
    Outcome,
    PolicyRequest,
    Priority,
    Queue,
    ReasonCode,
    TransactionRef,
)
from app.handoff import PolicyHandoffBuilder
from app.handoff.builder import SLOT_QUESTIONS
from app.policy.rules import REQUIRED_SLOTS
from tests.policy.conftest import (
    CONFIG,
    TXN_DATE,
    case,
    counters,
    evaluate,
    gate,
    request,
    slots,
    txn,
)

NO_SLOTS = {
    "card_in_possession": None,
    "shared_credentials": None,
    "duplicate_ref": None,
    "expected_amount": None,
    "expected_delivery_date": None,
    "merchant_contacted": None,
}
T3 = txn(amount="1500", amount_usd="1500")
HIGH_FRAUD = txn(fraud_score=91.5)
VELOCITY = [case(f"C-{i}", amount_usd="10") for i in range(3)]


def open_questions(req: PolicyRequest) -> list[str]:
    builder = PolicyHandoffBuilder(CONFIG.parameters, "k", lambda at: "HO-20261001-000001")
    return builder.build(
        request=req,
        decision=evaluate(req),
        language=Language.ES,
        customer_claims=[],
        actions_taken=[],
        open_questions=[],
        transcript_ref="CONV-1",
    ).open_questions


@pytest.mark.parametrize(
    ("label", "overrides", "rule", "queue"),
    [
        ("T3", {"transaction_candidates": [T3]}, "ESC-01", Queue.DISPUTES),
        ("fraud score", {"transaction_candidates": [HIGH_FRAUD]}, "ESC-04", Queue.FRAUD),
        ("velocity", {"cases": VELOCITY}, "ESC-02", Queue.DISPUTES),
    ],
)
@pytest.mark.parametrize("reason", [r for r in ReasonCode if REQUIRED_SLOTS[r]])
def test_record_trigger_escalates_before_asking_slots(
    label: str, overrides: dict[str, object], rule: str, queue: Queue, reason: ReasonCode
) -> None:
    req = request(slots=slots(reason_code=reason, **NO_SLOTS), **overrides)
    if reason is ReasonCode.DUPLICATE:  # an approved twin, so GATE-10 has a pair to ask about
        original = req.transaction_candidates[0]
        twin = original.model_copy(
            update={
                "transaction_id": "TXN-0",
                "transaction_date": original.transaction_date - timedelta(hours=2),
            }
        )
        req = req.model_copy(update={"transaction_candidates": [original, twin]})
    decision = evaluate(req)
    assert decision.outcome is Outcome.ESCALATE, label
    assert rule in decision.triggered_rules
    assert decision.queue is queue
    assert decision.clarify_target is None  # no question in the first turn
    questions = open_questions(req)
    for slot in REQUIRED_SLOTS[reason]:
        assert SLOT_QUESTIONS[slot] in questions, slot


def test_duplicate_case_comes_before_the_record_triggers() -> None:
    existing = case("CASE-9", transaction_id="TXN-1", status=CaseStatus.REJECTED)
    req = request(transaction_candidates=[T3], cases=[existing], slots=slots(expected_amount=None))
    decision = evaluate(req)
    assert decision.outcome is Outcome.INFORM
    assert decision.inform_reason is InformReason.DUPLICATE_CASE
    assert decision.triggered_rules == []
    assert gate(decision, "GATE-10") is None


def test_t1_without_triggers_still_asks_the_slots() -> None:
    decision = evaluate(request(slots=slots(reason_code=ReasonCode.NOT_RECEIVED, **NO_SLOTS)))
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.EXPECTED_DELIVERY_DATE
    assert decision.triggered_rules == []


def test_no_clarification_limit_is_spent_on_a_case_that_escalates() -> None:
    # The slot was already asked twice: a CLARIFY would be ESC-09, but no question is due.
    req = request(
        transaction_candidates=[T3],
        slots=slots(expected_amount=None),
        counters=counters(clarifications_by_slot={ClarifyTarget.EXPECTED_AMOUNT: 2}),
    )
    assert evaluate(req).triggered_rules == ["ESC-01"]


def test_escalations_of_gate10_on_given_slots_keep_their_route() -> None:
    # Shared credentials already stated: ESC-03 (fraud, high) is not lost behind ESC-01.
    req = request(
        transaction_candidates=[T3],
        slots=slots(reason_code=ReasonCode.UNRECOGNIZED, shared_credentials=True),
    )
    decision = evaluate(req)
    assert decision.triggered_rules == ["ESC-01", "ESC-03"]
    assert (decision.queue, decision.priority) == (Queue.FRAUD, Priority.HIGH)
    assert gate(decision, "GATE-10") is False


def test_gate10_inform_is_dropped_when_a_record_trigger_fires() -> None:
    # The amount agreed is not lower than the charge (an INFORM), but ESC-01 decides first.
    req = request(transaction_candidates=[T3], slots=slots(expected_amount=Decimal("2000")))
    decision = evaluate(req)
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-01"]
    assert decision.inform_reason is None


def duplicate_request(
    *, duplicate_ref: str | None = None, amount_usd: str = "50", **values: object
) -> PolicyRequest:
    """The customer names the original (TXN-1); the later twin TXN-2 already has a case."""
    first = txn("TXN-1", when=TXN_DATE, amount=amount_usd, amount_usd=amount_usd)
    second = txn(
        "TXN-2", when=TXN_DATE + timedelta(hours=3), amount=amount_usd, amount_usd=amount_usd
    )
    return request(
        transaction_candidates=[first, second],
        cases=[case("CASE-2", transaction_id="TXN-2")],
        slots=slots(
            reason_code=ReasonCode.DUPLICATE,
            transaction_ref=TransactionRef(transaction_id="TXN-1"),
            duplicate_ref=duplicate_ref,
            **values,
        ),
    )


def test_case_on_an_assumed_twin_is_confirmed_first() -> None:
    decision = evaluate(duplicate_request())
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.DUPLICATE_REF
    assert decision.duplicate_transaction_id == "TXN-2"
    assert decision.inform_reason is None and decision.existing_case_id is None
    assert "duplicate_case_pending_confirmation" in decision.notes


def test_case_on_a_confirmed_twin_is_reported() -> None:
    decision = evaluate(duplicate_request(duplicate_ref="TXN-2"))
    assert decision.inform_reason is InformReason.DUPLICATE_CASE
    assert decision.existing_case_id == "CASE-2"
    assert decision.transaction_id == "TXN-2"


def test_escalation_on_an_assumed_twin_stands() -> None:
    decision = evaluate(duplicate_request(amount_usd="1500"))
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-01"]
    assert decision.transaction_id == "TXN-2"


def test_rejected_assumed_twin_is_not_asked_again() -> None:
    from app.contracts import Confirmation

    declined = evaluate(duplicate_request(confirmation=Confirmation.DECLINED))
    assert declined.clarify_target is ClarifyTarget.REASON_CODE  # "another reason?"
    assert declined.existing_case_id is None
    withdrawn = evaluate(duplicate_request(confirmation=Confirmation.WITHDRAWN))
    assert withdrawn.inform_reason is InformReason.DISPUTE_WITHDRAWN


def test_case_on_the_charge_the_customer_named_is_reported_at_once() -> None:
    # The customer named the later charge: the case is on the transaction they pointed to.
    req = duplicate_request().model_copy(
        update={
            "slots": slots(
                reason_code=ReasonCode.DUPLICATE,
                transaction_ref=TransactionRef(transaction_id="TXN-2"),
            )
        }
    )
    decision = evaluate(req)
    assert decision.inform_reason is InformReason.DUPLICATE_CASE
    assert decision.existing_case_id == "CASE-2"


def test_dropped_inform_reason_reaches_the_agent() -> None:
    # amount_not_exceeded would be the GATE-10 outcome, but ESC-01 escalates first.
    req = request(transaction_candidates=[T3], slots=slots(expected_amount=Decimal("2000")))
    decision = evaluate(req)
    assert decision.triggered_rules == ["ESC-01"]
    assert "dropped_inform:amount_not_exceeded" in decision.notes
    assert any(
        q.startswith("Context: the expected amount the customer gave is not lower")
        for q in open_questions(req)
    )


def test_record_triggers_use_the_disputed_twin() -> None:
    first = txn("TXN-1", when=TXN_DATE, fraud_score=10.0)
    second = txn("TXN-2", when=TXN_DATE + timedelta(hours=3), fraud_score=91.5)
    req = request(
        transaction_candidates=[first, second],
        slots=slots(
            reason_code=ReasonCode.DUPLICATE, transaction_ref=TransactionRef(transaction_id="TXN-1")
        ),
    )
    decision = evaluate(req)
    assert decision.triggered_rules == ["ESC-04"]
    assert decision.transaction_id == "TXN-2"


def test_disputes_example_escalates_by_amount_again() -> None:
    from app.handoff.examples import example_packets

    packet = example_packets()["disputes"]
    assert packet.triggered_rules == ["ESC-01", "ESC-05"]
    assert "When were the goods or services due to be delivered?" in packet.open_questions
