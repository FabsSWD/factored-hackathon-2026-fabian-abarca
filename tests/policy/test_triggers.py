"""One test per escalation trigger of policy §7, plus tiers (§6) and the §4 table."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.contracts import (
    ActionId,
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    InformReason,
    InputGuardResult,
    ModelSignals,
    ModelSource,
    Outcome,
    Priority,
    ProvisionalCreditFlag,
    Queue,
    ReasonCode,
    Tier,
    ToolResult,
    ToolStatus,
)
from app.policy.rules import DISPUTABILITY, Mark
from tests.policy.conftest import (
    AS_OF,
    case,
    counters,
    evaluate,
    gate,
    request,
    slots,
    txn,
    unauthenticated,
    with_parameters,
)

CONFIRMED = slots(confirmation=Confirmation.CONFIRMED)


def confirmed(**overrides: object):  # type: ignore[no-untyped-def]
    return request(slots=CONFIRMED, **overrides)


# --- §6 tiers and ESC-01 ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount_usd", "tier", "flag"),
    [
        ("0.01", Tier.T1, ProvisionalCreditFlag.ELIGIBLE),
        ("100", Tier.T1, ProvisionalCreditFlag.ELIGIBLE),
        ("100.01", Tier.T2, ProvisionalCreditFlag.REQUIRES_REVIEW),
        ("1000", Tier.T2, ProvisionalCreditFlag.REQUIRES_REVIEW),
    ],
)
def test_tiers_resolve_with_the_flag(
    amount_usd: str, tier: Tier, flag: ProvisionalCreditFlag
) -> None:
    pool = [txn(amount="5000", amount_usd=amount_usd)]
    decision = evaluate(confirmed(transaction_candidates=pool))
    assert decision.outcome is Outcome.RESOLVE
    assert decision.tier is tier
    assert decision.provisional_credit_flag is flag
    assert decision.amount_usd == Decimal(amount_usd)


def test_esc01_above_auto_intake() -> None:
    decision = evaluate(
        confirmed(transaction_candidates=[txn(amount="5000", amount_usd="1000.01")])
    )
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-01"]
    assert decision.tier is Tier.T3
    assert decision.provisional_credit_flag is None
    assert decision.queue is Queue.DISPUTES and decision.priority is Priority.NORMAL


def test_esc01_unknown_usd_amount_is_t3() -> None:
    decision = evaluate(confirmed(transaction_candidates=[txn(amount_usd=None)]))
    assert decision.triggered_rules == ["ESC-01"]
    assert decision.tier is Tier.T3 and decision.amount_usd is None
    assert "amount_usd_missing" in decision.notes


# --- ESC-02 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("previous", "fires"), [("1950", False), ("1950.01", True)])
def test_esc02_disputed_total_includes_the_current_dispute(previous: str, fires: bool) -> None:
    decision = evaluate(confirmed(cases=[case(amount_usd=previous)]))  # current: USD 50
    assert ("ESC-02" in decision.triggered_rules) is fires


def test_esc02_total_window_is_30_days() -> None:
    inside = case(amount_usd="1960", business_created_at=AS_OF - timedelta(days=30))
    outside = case(amount_usd="1960", business_created_at=AS_OF - timedelta(days=30, seconds=1))
    assert "ESC-02" in evaluate(confirmed(cases=[inside])).triggered_rules
    assert evaluate(confirmed(cases=[outside])).outcome is Outcome.RESOLVE


@pytest.mark.parametrize(("count", "fires"), [(2, False), (3, True)])
def test_esc02_previous_cases_in_90_days(count: int, fires: bool) -> None:
    previous = [
        case(f"CASE-{i}", amount_usd="10", business_created_at=AS_OF - timedelta(days=80))
        for i in range(count)
    ]
    assert ("ESC-02" in evaluate(confirmed(cases=previous)).triggered_rules) is fires


def test_esc02_count_window_is_90_days() -> None:
    old = [
        case(f"CASE-{i}", amount_usd="10", business_created_at=AS_OF - timedelta(days=91))
        for i in range(3)
    ]
    assert evaluate(confirmed(cases=old)).outcome is Outcome.RESOLVE


def test_esc02_drafts_never_count() -> None:
    drafts = [case(f"CASE-{i}", status=CaseStatus.DRAFT, amount_usd="1999") for i in range(3)]
    assert evaluate(confirmed(cases=drafts)).outcome is Outcome.RESOLVE


def test_esc02_closed_cases_count() -> None:
    closed = [case(f"CASE-{i}", status=CaseStatus.CLOSED, amount_usd="10") for i in range(3)]
    assert evaluate(confirmed(cases=closed)).triggered_rules == ["ESC-02"]


# --- ESC-03 ----------------------------------------------------------------------------------


def test_esc03_account_takeover_statement() -> None:
    decision = evaluate(request(flags={"account_takeover_reported": True}))
    assert decision.triggered_rules == ["ESC-03"]
    assert decision.queue is Queue.FRAUD and decision.priority is Priority.HIGH


@pytest.mark.parametrize(("count", "fires"), [(2, False), (3, True)])
def test_esc03_unrecognized_batch(count: int, fires: bool) -> None:
    decision = evaluate(request(counters=counters(unrecognized_transactions=count)))
    assert ("ESC-03" in decision.triggered_rules) is fires


def test_esc03_fires_before_authentication() -> None:
    decision = evaluate(unauthenticated(flags={"account_takeover_reported": True}))
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-03"]
    assert ActionId.BLOCK_CARD not in decision.authorized_actions


# --- ESC-04 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("score", "fires"), [(34.99, False), (35.0, True), (99.0, True)])
def test_esc04_fraud_score_threshold(score: float, fires: bool) -> None:
    decision = evaluate(confirmed(transaction_candidates=[txn(fraud_score=score)]))
    assert ("ESC-04" in decision.triggered_rules) is fires
    if fires:
        assert decision.queue is Queue.FRAUD and decision.priority is Priority.NORMAL


def test_esc04_null_score_does_not_escalate_and_is_noted() -> None:
    decision = evaluate(confirmed(transaction_candidates=[txn(fraud_score=None)]))
    assert decision.outcome is Outcome.RESOLVE
    assert "fraud_score_missing" in decision.notes


def test_esc04_is_record_dependent_and_waits_for_the_gates() -> None:
    # Policy §5 example: a pending transaction with fraud_score 90 ends in INFORM.
    decision = evaluate(request(transaction_candidates=[txn(status="Pending", fraud_score=90)]))
    assert decision.outcome is Outcome.INFORM
    assert decision.triggered_rules == []


# --- ESC-05, ESC-06 --------------------------------------------------------------------------


def test_esc05_human_requested() -> None:
    decision = evaluate(request(flags={"human_requested": True}))
    assert decision.triggered_rules == ["ESC-05"]
    assert decision.queue is Queue.DISPUTES and decision.priority is Priority.NORMAL
    assert decision.authorized_actions == [ActionId.TRANSFER_TO_HUMAN]


def test_esc06_legal_or_vulnerability() -> None:
    decision = evaluate(request(flags={"legal_or_vulnerability": True}))
    assert decision.triggered_rules == ["ESC-06"]
    assert decision.queue is Queue.DISPUTES and decision.priority is Priority.HIGH


@pytest.mark.parametrize("flag", ["human_requested", "legal_or_vulnerability"])
def test_esc05_esc06_fire_before_authentication(flag: str) -> None:
    decision = evaluate(unauthenticated(flags={flag: True}))
    assert decision.outcome is Outcome.ESCALATE


# --- ESC-07, ESC-08, ESC-12, ESC-14 are gate outcomes (see test_gates.py) ---------------------


def test_esc14_disputes_queue_for_withdrawal_duplicate() -> None:
    pool = [txn(transaction_type="Withdrawal")]
    decision = evaluate(
        request(transaction_candidates=pool, slots=slots(reason_code=ReasonCode.DUPLICATE))
    )
    assert decision.triggered_rules == ["ESC-14"]
    assert decision.queue is Queue.DISPUTES


@pytest.mark.parametrize("transaction_type", ["Transfer", "Payment"])
def test_esc14_fraud_queue_for_unrecognized_transfers_and_payments(transaction_type: str) -> None:
    pool = [txn(transaction_type=transaction_type)]
    req = request(transaction_candidates=pool, slots=slots(reason_code=ReasonCode.UNRECOGNIZED))
    decision = evaluate(req)
    assert decision.triggered_rules == ["ESC-14"]
    assert decision.queue is Queue.FRAUD


def test_esc14_disputes_queue_for_transfer_duplicate() -> None:
    pool = [txn(transaction_type="Transfer")]
    req = request(transaction_candidates=pool, slots=slots(reason_code=ReasonCode.DUPLICATE))
    assert evaluate(req).queue is Queue.DISPUTES


# --- §4 disputability table ------------------------------------------------------------------


COMBINATIONS = [
    (transaction_type, reason, mark)
    for transaction_type, row in DISPUTABILITY.items()
    for reason, mark in row.items()
]


def test_table_has_six_types_by_five_reasons() -> None:
    assert len(COMBINATIONS) == 30


@pytest.mark.parametrize(
    "expected",
    [
        ("Purchase", "AAAAN"),
        ("Withdrawal", "AHHHN"),
        ("Transfer", "HHNNN"),
        ("Payment", "HHNNN"),
        ("Deposit", "NNNNN"),
        ("Adjustment", "NANNA"),
    ],
)
def test_table_matches_policy_section_4(expected: tuple[str, str]) -> None:
    transaction_type, marks = expected
    order = [
        ReasonCode.UNRECOGNIZED,
        ReasonCode.DUPLICATE,
        ReasonCode.INCORRECT_AMOUNT,
        ReasonCode.NOT_RECEIVED,
        ReasonCode.FEE,
    ]
    assert "".join(DISPUTABILITY[transaction_type][r] for r in order) == marks


@pytest.mark.parametrize(("transaction_type", "reason", "mark"), COMBINATIONS)
def test_disputability_outcomes(transaction_type: str, reason: ReasonCode, mark: Mark) -> None:
    req = request(
        transaction_candidates=[txn(transaction_type=transaction_type)],
        slots=slots(reason_code=reason),
    )
    decision = evaluate(req)
    assert gate(decision, "GATE-07") is (mark is Mark.AUTOMATED)
    if mark is Mark.HUMAN:
        assert decision.outcome is Outcome.ESCALATE
        assert decision.triggered_rules == ["ESC-14"]
    if mark is Mark.NOT_DISPUTABLE:
        assert decision.outcome is Outcome.INFORM
        assert decision.inform_reason is InformReason.NOT_DISPUTABLE


# --- ESC-09 ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("asked", "fires"), [(1, False), (2, True)])
def test_esc09_per_slot_limit(asked: int, fires: bool) -> None:
    req = request(
        slots=slots(expected_amount=None),
        counters=counters(clarifications_by_slot={ClarifyTarget.EXPECTED_AMOUNT: asked}),
    )
    decision = evaluate(req)
    assert (decision.triggered_rules == ["ESC-09"]) is fires
    if not fires:
        assert decision.clarify_target is ClarifyTarget.EXPECTED_AMOUNT


@pytest.mark.parametrize(("total", "fires"), [(3, False), (4, True)])
def test_esc09_total_limit(total: int, fires: bool) -> None:
    req = request(slots=slots(expected_amount=None), counters=counters(total_clarifications=total))
    assert (evaluate(req).triggered_rules == ["ESC-09"]) is fires


def test_esc09_summary_presentation_is_not_a_clarification() -> None:
    decision = evaluate(request(counters=counters(total_clarifications=4)))
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.CONFIRMATION


def test_esc09_hedged_confirmation_counts() -> None:
    req = request(
        slots=slots(confirmation=Confirmation.HEDGED), counters=counters(total_clarifications=4)
    )
    assert evaluate(req).triggered_rules == ["ESC-09"]


def test_esc09_unresolved_contradiction() -> None:
    decision = evaluate(request(counters=counters(unresolved_contradiction=True)))
    assert decision.triggered_rules == ["ESC-09"]


def test_esc09_limits_do_not_apply_to_language_or_authentication() -> None:
    limits = counters(total_clarifications=9)
    assert evaluate(unauthenticated(counters=limits)).clarify_target is ClarifyTarget.AUTHENTICATION
    lang = unauthenticated(detected_language="en", counters=limits)
    assert evaluate(lang).clarify_target is ClarifyTarget.LANGUAGE


# --- ESC-10 ----------------------------------------------------------------------------------


def tool(action: ActionId, status: ToolStatus) -> ToolResult:
    success = status is ToolStatus.SUCCESS
    return ToolResult(
        action=action,
        status=status,
        verified=success,
        attempts=3,
        idempotency_key="TXN-1:RC_INCORRECT_AMOUNT" if action is ActionId.CREATE_CASE else None,
        record_id="REC-1" if success else None,
        error=None if success else "timeout",
    )


@pytest.mark.parametrize("action", [ActionId.CREATE_CASE, ActionId.BLOCK_CARD])
def test_esc10_failed_write_action(action: ActionId) -> None:
    decision = evaluate(confirmed(tool_results=[tool(action, ToolStatus.FAILED)]))
    assert decision.triggered_rules == ["ESC-10"]


def test_esc10_access_denied_write_counts_as_failure() -> None:
    result = ToolResult(
        action=ActionId.BLOCK_CARD, status=ToolStatus.ACCESS_DENIED, verified=False, attempts=1
    )
    assert evaluate(request(tool_results=[result])).triggered_rules == ["ESC-10"]


def test_esc10_ignores_successful_writes_and_handoff_failures() -> None:
    results = [
        tool(ActionId.BLOCK_CARD, ToolStatus.SUCCESS),
        tool(ActionId.TRANSFER_TO_HUMAN, ToolStatus.FAILED),
    ]
    assert evaluate(request(tool_results=results)).triggered_rules == []


# --- ESC-11 ----------------------------------------------------------------------------------


UNAVAILABLE = ModelSignals(source=ModelSource.UNAVAILABLE)


def kev(top: float = 0.9, risk: float = 0.1) -> ModelSignals:
    return ModelSignals(
        source=ModelSource.KEV,
        reason_code_probs={ReasonCode.INCORRECT_AMOUNT: top},
        reason_code_other=round(1 - top, 4),
        ambiguity=0.1,
        escalation_risk=risk,
    )


def test_esc11_null_thresholds_fire_only_when_signals_are_unavailable() -> None:
    assert evaluate(request(signals=UNAVAILABLE)).triggered_rules == ["ESC-11"]
    assert evaluate(request(signals=kev(top=0.2, risk=0.99))).triggered_rules == []


def test_esc11_unavailable_signals_block_a_resolve() -> None:
    decision = evaluate(confirmed(signals=UNAVAILABLE))
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-11"]


THRESHOLDS = with_parameters(DECISION_CONFIDENCE_MIN=0.6, ESCALATION_RISK_THRESHOLD=0.7)


@pytest.mark.parametrize(
    ("signals", "fires"),
    [
        (kev(top=0.59), True),
        (kev(top=0.6), False),
        (kev(risk=0.7), True),
        (kev(risk=0.69), False),
    ],
)
def test_esc11_thresholds_apply_to_kev(signals: ModelSignals, fires: bool) -> None:
    decision = evaluate(confirmed(signals=signals), THRESHOLDS)
    assert (decision.triggered_rules == ["ESC-11"]) is fires


def test_esc11_kev_without_reason_probabilities_is_uncertain() -> None:
    signals = ModelSignals(source=ModelSource.KEV, reason_code_other=1.0, escalation_risk=0.1)
    assert evaluate(confirmed(signals=signals), THRESHOLDS).triggered_rules == ["ESC-11"]


def test_esc11_single_threshold() -> None:
    risk_only = with_parameters(ESCALATION_RISK_THRESHOLD=0.7)
    assert evaluate(confirmed(signals=kev(top=0.1)), risk_only).outcome is Outcome.RESOLVE
    assert evaluate(confirmed(signals=kev(risk=0.8)), risk_only).triggered_rules == ["ESC-11"]


def test_esc11_never_fires_on_the_fallback() -> None:
    fallback = ModelSignals(
        source=ModelSource.LLM_FALLBACK, reason_code_probs={}, ambiguity=1.0, escalation_risk=1.0
    )
    assert evaluate(confirmed(signals=fallback), THRESHOLDS).outcome is Outcome.RESOLVE


def test_esc11_only_when_no_hard_rule_decided() -> None:
    decision = evaluate(request(signals=UNAVAILABLE, flags={"human_requested": True}))
    assert decision.triggered_rules == ["ESC-05"]
    pending = request(signals=UNAVAILABLE, transaction_candidates=[txn(status="Pending")])
    assert evaluate(pending).outcome is Outcome.INFORM


def test_esc11_only_after_authentication() -> None:
    decision = evaluate(unauthenticated(signals=UNAVAILABLE))
    assert decision.outcome is Outcome.CLARIFY
    assert decision.clarify_target is ClarifyTarget.AUTHENTICATION


# --- ESC-13 ----------------------------------------------------------------------------------


def test_esc13_first_attempt_is_ignored() -> None:
    guard = InputGuardResult(flagged=True, strikes=1, pattern_id="override")
    decision = evaluate(request(input_guard=guard, counters=counters(injection_strikes=1)))
    assert "ESC-13" not in decision.triggered_rules


def test_esc13_second_attempt_escalates_to_security_review() -> None:
    guard = InputGuardResult(flagged=True, strikes=2, pattern_id="override", escalate_security=True)
    decision = evaluate(request(input_guard=guard))
    assert decision.triggered_rules == ["ESC-13"]
    assert decision.queue is Queue.SECURITY_REVIEW


def test_esc13_from_the_conversation_counter() -> None:
    decision = evaluate(unauthenticated(counters=counters(injection_strikes=2)))
    assert decision.triggered_rules == ["ESC-13"]


# --- Several triggers ------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("flags", "extra", "queue", "priority"),
    [
        (
            {"human_requested": True, "legal_or_vulnerability": True},
            {},
            Queue.DISPUTES,
            Priority.HIGH,
        ),
        (
            {"human_requested": True, "account_takeover_reported": True},
            {},
            Queue.FRAUD,
            Priority.HIGH,
        ),
        (
            {"account_takeover_reported": True},
            {"counters": counters(injection_strikes=2)},
            Queue.SECURITY_REVIEW,
            Priority.HIGH,
        ),
        (
            {"human_requested": True},
            {"counters": counters(injection_strikes=2)},
            Queue.SECURITY_REVIEW,
            Priority.NORMAL,
        ),
    ],
)
def test_several_triggers_most_restrictive_queue_and_any_high_priority(
    flags: dict[str, bool], extra: dict[str, object], queue: Queue, priority: Priority
) -> None:
    decision = evaluate(request(flags=flags, **extra))
    assert decision.queue is queue
    assert decision.priority is priority
    assert len(decision.triggered_rules) == 2


def test_record_triggers_are_all_listed() -> None:
    pool = [txn(amount="5000", amount_usd="5000", fraud_score=80)]
    decision = evaluate(confirmed(transaction_candidates=pool))
    assert decision.triggered_rules == ["ESC-01", "ESC-02", "ESC-04"]
    assert decision.queue is Queue.FRAUD
