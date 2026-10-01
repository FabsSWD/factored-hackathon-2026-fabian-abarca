"""Evidence of every fired rule: copied from the request, with its origin."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.contracts import (
    ActionId,
    ClarifyTarget,
    Confirmation,
    Evidence,
    EvidenceKind,
    ModelSignals,
    ModelSource,
    PolicyDecision,
    PolicyRequest,
    ReasonCode,
    ToolResult,
    ToolStatus,
)
from app.policy.engine import _State
from tests.policy.conftest import (
    case,
    counters,
    customer,
    evaluate,
    product,
    request,
    slots,
    txn,
    unauthenticated,
    with_parameters,
)

CONFIRMED = slots(confirmation=Confirmation.CONFIRMED)


def evidence(decision: PolicyDecision, rule: str) -> list[Evidence]:
    return next(item.evidence for item in decision.evidence if item.rule_id == rule)


def pairs(decision: PolicyDecision, rule: str) -> list[tuple[str, str, str]]:
    return [(e.kind.value, e.name, e.value) for e in evidence(decision, rule)]


def test_every_fired_rule_has_evidence_in_rule_order() -> None:
    decision = evaluate(
        request(
            flags={"human_requested": True, "legal_or_vulnerability": True},
            counters=counters(injection_strikes=2),
        )
    )
    assert [e.rule_id for e in decision.evidence] == decision.triggered_rules
    assert decision.triggered_rules == ["ESC-05", "ESC-06", "ESC-13"]


def test_firing_without_evidence_is_a_bug() -> None:
    with pytest.raises(ValueError, match="without evidence"):
        _State().fire("ESC-05", [])


def test_repeated_firing_merges_evidence_without_duplicates() -> None:
    state = _State()
    item = Evidence(kind=EvidenceKind.COUNTER, name="x", value="1", origin="conversation counter")
    state.fire("ESC-09", [item])
    state.fire("ESC-09", [item])
    assert state.evidence["ESC-09"] == [item]


@pytest.mark.parametrize(
    ("req", "rule", "expected"),
    [
        (
            request(
                flags={"account_takeover_reported": True},
                counters=counters(unrecognized_transactions=3),
            ),
            "ESC-03",
            [
                ("flag", "account_takeover_reported", "true"),
                ("counter", "unrecognized_transactions", "3 (limit 3)"),
            ],
        ),
        (
            request(slots=slots(reason_code=ReasonCode.UNRECOGNIZED, shared_credentials=True)),
            "ESC-03",
            [("slot", "shared_credentials", "yes")],
        ),
        (
            request(
                slots=CONFIRMED, transaction_candidates=[txn(amount="5000", amount_usd="1500")]
            ),
            "ESC-01",
            [("record", "amount_usd", "1500 (above 1000)")],
        ),
        (
            request(slots=CONFIRMED, transaction_candidates=[txn(amount_usd=None)]),
            "ESC-01",
            [("record", "amount_usd", "unknown (treated as T3)")],
        ),
        (
            request(slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)]),
            "ESC-04",
            [("record", "fraud_score", "91.5 (threshold 35)")],
        ),
        (
            request(transaction_candidates=[txn(when=datetime(2026, 3, 1, 12))]),
            "ESC-07",
            [
                (
                    "record",
                    "transaction_date",
                    "2026-03-01, 108 days before the business date (window 60, late window 120)",
                )
            ],
        ),
        (
            request(customer=customer("Suspended")),
            "ESC-08",
            [("record", "customer_status", "Suspended")],
        ),
        (
            request(customer=None),
            "ESC-08",
            [("record", "customer_status", "missing")],
        ),
        (
            request(products=[product(status="Closed")]),
            "ESC-08",
            [("record", "product_status", "Closed")],
        ),
        (
            request(products=[]),
            "ESC-08",
            [("record", "product_status", "missing")],
        ),
        (
            request(counters=counters(unresolved_contradiction=True)),
            "ESC-09",
            [("counter", "unresolved_contradiction", "true")],
        ),
        (
            request(
                slots=slots(expected_amount=None),
                counters=counters(
                    clarifications_by_slot={ClarifyTarget.EXPECTED_AMOUNT: 2},
                    total_clarifications=4,
                ),
            ),
            "ESC-09",
            [
                ("counter", "clarifications_by_slot.expected_amount", "2 (limit 2)"),
                ("counter", "total_clarifications", "4 (limit 4)"),
            ],
        ),
        (
            request(
                slots=slots(reason_code=ReasonCode.DUPLICATE),
                counters=counters(duplicate_reason_reasked=True),
            ),
            "ESC-09",
            [
                ("counter", "duplicate_reason_reasked", "true"),
                ("slot", "reason_code", "RC_DUPLICATE"),
            ],
        ),
        (
            request(
                tool_results=[
                    ToolResult(
                        action=ActionId.BLOCK_CARD,
                        status=ToolStatus.FAILED,
                        verified=False,
                        attempts=3,
                        error="timeout",
                    )
                ]
            ),
            "ESC-10",
            [("tool_result", "ACT-03", "failed: timeout")],
        ),
        (
            request(
                tool_results=[
                    ToolResult(
                        action=ActionId.BLOCK_CARD,
                        status=ToolStatus.ACCESS_DENIED,
                        verified=False,
                        attempts=1,
                    )
                ]
            ),
            "ESC-10",
            [("tool_result", "ACT-03", "access_denied")],
        ),
        (
            request(signals=ModelSignals(source=ModelSource.UNAVAILABLE)),
            "ESC-11",
            [("signal", "source", "unavailable")],
        ),
        (
            unauthenticated(
                detected_language="es",
                language_ambiguous=True,
                counters=counters(language_clarifications=1),
            ),
            "ESC-12",
            [
                ("language", "detected_language", "es (ambiguous)"),
                ("counter", "language_clarifications", "1"),
            ],
        ),
        (
            unauthenticated(detected_language=None, counters=counters(language_clarifications=1)),
            "ESC-12",
            [
                ("language", "detected_language", "unknown"),
                ("counter", "language_clarifications", "1"),
            ],
        ),
        (
            request(counters=counters(injection_strikes=2)),
            "ESC-13",
            [("counter", "injection_strikes", "2 (limit 2)")],
        ),
        (
            request(
                transaction_candidates=[txn(transaction_type="Transfer")],
                slots=slots(reason_code=ReasonCode.UNRECOGNIZED),
            ),
            "ESC-14",
            [
                ("record", "transaction_type", "Transfer"),
                ("slot", "reason_code", "RC_UNRECOGNIZED"),
            ],
        ),
    ],
)
def test_evidence_of_each_rule(
    req: PolicyRequest, rule: str, expected: list[tuple[str, str, str]]
) -> None:
    assert pairs(evaluate(req), rule) == expected


def test_velocity_evidence_names_both_windows() -> None:
    previous = [case(f"C-{i}", amount_usd="700") for i in range(3)]
    decision = evaluate(request(slots=CONFIRMED, cases=previous))
    assert pairs(decision, "ESC-02") == [
        ("record", "disputed_usd_30d", "2150 including this dispute (limit 2000)"),
        ("record", "cases_90d", "3 previous cases (limit 3)"),
    ]
    assert {e.source for e in evidence(decision, "ESC-02")} == {"cases"}


def test_record_evidence_names_its_table_and_record() -> None:
    decision = evaluate(request(slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)]))
    (item,) = evidence(decision, "ESC-04")
    assert (item.source, item.record_id, item.origin) == ("transactions", "TXN-1", "Core Banking")


def test_flag_origins_say_whether_a_rule_detector_backs_them() -> None:
    decision = evaluate(request(flags={"human_requested": True, "account_takeover_reported": True}))
    assert (
        evidence(decision, "ESC-05")[0].origin
        == "customer statement (rule detector and LLM extraction)"
    )
    assert evidence(decision, "ESC-03")[0].origin == "customer statement (LLM extraction)"


def test_kev_uncertainty_evidence() -> None:
    config = with_parameters(DECISION_CONFIDENCE_MIN=0.6, ESCALATION_RISK_THRESHOLD=0.7)
    kev = ModelSignals(
        source=ModelSource.KEV,
        reason_code_probs={ReasonCode.INCORRECT_AMOUNT: 0.5},
        reason_code_other=0.5,
        escalation_risk=0.8,
    )
    decision = evaluate(request(signals=kev, slots=CONFIRMED), config)
    assert pairs(decision, "ESC-11") == [
        ("signal", "top_reason_code", "RC_INCORRECT_AMOUNT 0.5 (minimum 0.6)"),
        ("signal", "escalation_risk", "0.8 (threshold 0.7)"),
    ]
    no_probs = ModelSignals(source=ModelSource.KEV, reason_code_other=1.0, escalation_risk=0.1)
    decision = evaluate(request(signals=no_probs, slots=CONFIRMED), config)
    assert pairs(decision, "ESC-11") == [("signal", "top_reason_code", "none (minimum 0.6)")]


def test_input_guard_pattern_is_part_of_the_evidence() -> None:
    from app.contracts import InputGuardResult

    guard = InputGuardResult(flagged=True, strikes=2, pattern_id="override", escalate_security=True)
    decision = evaluate(request(input_guard=guard))
    assert pairs(decision, "ESC-13") == [
        ("counter", "injection_strikes", "2 (limit 2)"),
        ("input_guard", "pattern_id", "override"),
    ]
