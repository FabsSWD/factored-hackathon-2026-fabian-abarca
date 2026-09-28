from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from app import contracts as c

NOW = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)
AS_OF = datetime(2026, 6, 18, 6, 0)  # naive, dataset clock
CUSTOMER = "CLI-AAA"
OTHER = "CLI-BBB"


# --- Builders ---------------------------------------------------------------


def session(**overrides: Any) -> c.SessionContext:
    data: dict[str, Any] = {
        "session_id": "SES-1",
        "customer_id": CUSTOMER,
        "auth_method": "test_otp",
        "issued_at": NOW - timedelta(minutes=5),
        "last_activity_at": NOW - timedelta(minutes=1),
    }
    return c.SessionContext(**(data | overrides))


def transaction(**overrides: Any) -> c.TransactionRecord:
    data: dict[str, Any] = {
        "transaction_id": "TXN-1",
        "customer_id": CUSTOMER,
        "product_id": "PRD-1",
        "transaction_type": "Purchase",
        "transaction_status": "Approved",
        "transaction_date": AS_OF - timedelta(days=3),
        "amount": Decimal("1250.00"),
        "currency": "MXN",
        "amount_usd": Decimal("68.40"),
        "merchant_name": "MERCHANT_X",
        "fraud_score": 12.5,
    }
    return c.TransactionRecord(**(data | overrides))


def product(**overrides: Any) -> c.ProductRecord:
    data: dict[str, Any] = {
        "product_id": "PRD-1",
        "customer_id": CUSTOMER,
        "product_type": "Tarjeta Crédito",
        "product_number_masked": "****4821",
        "currency": "MXN",
        "product_status": "Active",
    }
    return c.ProductRecord(**(data | overrides))


def case(**overrides: Any) -> c.CaseRecord:
    data: dict[str, Any] = {
        "case_id": "CASE-1",
        "customer_id": CUSTOMER,
        "transaction_id": "TXN-1",
        "reason_code": c.ReasonCode.UNRECOGNIZED,
        "status": c.CaseStatus.OPEN,
        "tier": c.Tier.T1,
        "amount": Decimal("1250.00"),
        "currency": "MXN",
        "amount_usd": Decimal("68.40"),
        "provisional_credit_flag": c.ProvisionalCreditFlag.ELIGIBLE,
        "created_at": NOW,
    }
    return c.CaseRecord(**(data | overrides))


def decision(**overrides: Any) -> c.PolicyDecision:
    data: dict[str, Any] = {
        "outcome": c.Outcome.RESOLVE,
        "policy_version": "0.1.0",
        "tier": c.Tier.T1,
        "provisional_credit_flag": c.ProvisionalCreditFlag.ELIGIBLE,
        "authorized_actions": [c.ActionId.CREATE_CASE, c.ActionId.RECORD_CREDIT_FLAG],
        "transaction_id": "TXN-1",
        "reason_code": c.ReasonCode.UNRECOGNIZED,
        "gates_evaluated": [c.GateResult(gate_id="GATE-01", passed=True)],
    }
    return c.PolicyDecision(**(data | overrides))


def tool_result(**overrides: Any) -> c.ToolResult:
    data: dict[str, Any] = {
        "action": c.ActionId.CREATE_CASE,
        "status": c.ToolStatus.SUCCESS,
        "verified": True,
        "attempts": 1,
        "idempotency_key": "TXN-1:RC_UNRECOGNIZED",
        "record_id": "CASE-1",
    }
    return c.ToolResult(**(data | overrides))


POLICY_HANDOFF_EXAMPLE: dict[str, Any] = {
    "handoff_id": "HO-20260925-000123",
    "created_at": "2026-09-25T22:45:00Z",
    "language": "pt",
    "queue": "fraud",
    "priority": "high",
    "customer_ref": "CUS-pseudonym-7f3a",
    "auth": {"status": "authenticated", "method": "test_otp", "session_age_min": 6},
    "request_summary": "Customer disputes an unrecognized purchase and reports a lost phone.",
    "reason_code": "RC_UNRECOGNIZED",
    "triggered_rules": ["ESC-03"],
    "verified_facts": [
        {
            "fact": "Purchase of 1,250.00 MXN at MERCHANT_X on 2026-06-10, status Approved",
            "source": "transactions",
            "record_id": "TXN-...",
        },
        {"fact": "Card ending 4821 is Active", "source": "products", "record_id": "PRD-..."},
    ],
    "customer_claims": ["Did not make the purchase", "Phone was stolen on 2026-06-09"],
    "actions_taken": [
        {
            "action": "ACT-03",
            "result": "success",
            "verified": True,
            "detail": "Card ending 4821 blocked",
        }
    ],
    "draft_case": {
        "transaction_ref": "TXN-...",
        "amount_usd": 68.40,
        "tier": "T1",
        "provisional_credit_flag": "eligible",
    },
    "model_signals": {
        "reason_code_probs": {"RC_UNRECOGNIZED": 0.94, "RC_DUPLICATE": 0.03},
        "escalation_risk": 0.81,
        "model_version": "decision-layer@0.1.0",
    },
    "open_questions": ["Were other transactions made after 2026-06-09?"],
    "transcript_ref": "CONV-...",
    "policy_version": "0.1.0",
}


# --- Enums ------------------------------------------------------------------


def test_enum_values_match_the_policy() -> None:
    assert [o.value for o in c.Outcome] == ["RESOLVE", "CLARIFY", "INFORM", "ESCALATE", "REFUSE"]
    assert {r.value for r in c.ReasonCode} == {
        "RC_UNRECOGNIZED",
        "RC_DUPLICATE",
        "RC_INCORRECT_AMOUNT",
        "RC_NOT_RECEIVED",
        "RC_FEE",
    }
    assert [t.value for t in c.Tier] == ["T1", "T2", "T3"]
    assert {lang.value for lang in c.Language} == {"es", "pt"}
    assert {a.value for a in c.ActionId} == {"ACT-01", "ACT-02", "ACT-03", "ACT-04", "ACT-05"}
    assert c.WRITE_ACTIONS == {c.ActionId.CREATE_CASE, c.ActionId.BLOCK_CARD}


def test_prohibited_action_act_06_cannot_be_expressed() -> None:
    with pytest.raises(ValueError, match="ACT-06"):
        c.ActionId("ACT-06")


def test_slot_order_follows_section_10() -> None:
    order = list(c.SlotName)
    assert order[:2] == [c.SlotName.TRANSACTION_REF, c.SlotName.REASON_CODE]
    assert order[-1] is c.SlotName.CONFIRMATION
    assert set(c.Slots.model_fields) == {s.value for s in c.SlotName}


def test_every_slot_is_a_clarify_target() -> None:
    targets = {t.value for t in c.ClarifyTarget}
    assert {s.value for s in c.SlotName} <= targets
    assert targets - {s.value for s in c.SlotName} == {"language", "authentication"}


def test_case_statuses_follow_the_lifecycle_diagram() -> None:
    assert {s.value for s in c.CaseStatus} == {
        "Draft",
        "Open",
        "Escalated",
        "In Process",
        "Resolved",
        "Rejected",
        "Closed",
    }


def test_invalid_enum_value_is_rejected() -> None:
    with pytest.raises(ValidationError):
        c.Slots(reason_code="RC_OTHER")  # type: ignore[arg-type]


# --- Base behaviour ---------------------------------------------------------


ALL_CONTRACTS = [
    obj
    for obj in vars(c).values()
    if isinstance(obj, type) and issubclass(obj, BaseModel) and obj not in (BaseModel, c.Contract)
]


@pytest.mark.parametrize("model", ALL_CONTRACTS, ids=lambda m: m.__name__)
def test_every_contract_forbids_extra_fields_and_is_frozen(model: type[BaseModel]) -> None:
    assert issubclass(model, c.Contract)
    assert model.model_config.get("extra") == "forbid"
    assert model.model_config.get("frozen") is True


def test_extra_field_is_rejected() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        c.CustomerRecord(customer_id=CUSTOMER, customer_status="Active", gender="F")  # type: ignore[call-arg]


def test_instances_are_immutable() -> None:
    record = transaction()
    with pytest.raises(ValidationError, match="frozen"):
        record.amount = Decimal("1")  # type: ignore[misc]


# --- Verified records -------------------------------------------------------


def test_valid_records() -> None:
    assert transaction().amount == Decimal("1250.00")
    assert product().product_number_masked == "****4821"
    assert case().status is c.CaseStatus.OPEN
    fact = c.VerifiedFact(fact="Card ending 4821 is Active", source="products", record_id="PRD-1")
    assert fact.source == "products"


@pytest.mark.parametrize("field", ["transaction_id", "customer_id", "transaction_status"])
def test_transaction_required_fields(field: str) -> None:
    data = transaction().model_dump()
    del data[field]
    with pytest.raises(ValidationError, match=field):
        c.TransactionRecord(**data)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("amount", Decimal("0")),
        ("amount", Decimal("-5")),
        ("amount", Decimal("NaN")),
        ("currency", "mxn"),
        ("currency", "MXNN"),
        ("fraud_score", 101),
        ("fraud_score", -1),
        ("transaction_date", datetime(2026, 1, 1, tzinfo=UTC)),  # aware: wrong clock
        ("amount_usd", Decimal("-1")),
        ("transaction_id", ""),
    ],
)
def test_transaction_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        transaction(**{field: value})


@pytest.mark.parametrize("number", ["4332181960", "4959310341316475", "*4821x", "****482"])
def test_product_number_must_be_masked(number: str) -> None:
    with pytest.raises(ValidationError):
        product(product_number_masked=number)


@pytest.mark.parametrize("number", ["4821", "****4821", "*4821"])
def test_masked_product_numbers_accepted(number: str) -> None:
    assert product(product_number_masked=number).product_number_masked == number


def test_verified_fact_requires_source_and_record_id() -> None:
    with pytest.raises(ValidationError):
        c.VerifiedFact(fact="x", source="", record_id="R")
    with pytest.raises(ValidationError):
        c.VerifiedFact(fact="x", source="transactions")  # type: ignore[call-arg]


def test_customer_record_has_no_prohibited_fields() -> None:
    # DATA-02: these fields must not be able to reach a decision.
    prohibited = {
        "segment",
        "credit_score",
        "estimated_monthly_income",
        "gender",
        "date_of_birth",
        "detected_accent",
        "occupation",
        "marital_status",
        "education_level",
        "country",
        "document_number",
        "first_name",
        "last_name",
        "email",
        "mobile_phone",
        "address",
    }
    for model in (c.CustomerRecord, c.PolicyRequest, c.LLMContext, c.HandoffPacket):
        assert not prohibited & set(model.model_fields), model.__name__


def test_dispute_history_rejects_negative_values() -> None:
    with pytest.raises(ValidationError):
        c.DisputeHistory(disputed_usd_last_30d=Decimal("-1"), cases_last_90d=0)
    with pytest.raises(ValidationError):
        c.DisputeHistory(disputed_usd_last_30d=Decimal("0"), cases_last_90d=-1)


# --- Slots and flags --------------------------------------------------------


def test_empty_slots_are_all_unfilled() -> None:
    assert all(value is None for value in c.Slots().model_dump().values())


def test_slots_with_values() -> None:
    slots = c.Slots(
        transaction_ref=c.TransactionRef(transaction_id="TXN-1"),
        reason_code=c.ReasonCode.NOT_RECEIVED,
        expected_delivery_date=date(2026, 9, 1),
        merchant_contacted=True,
        confirmation=c.Confirmation.HEDGED,
    )
    assert slots.reason_code is c.ReasonCode.NOT_RECEIVED
    assert slots.confirmation is c.Confirmation.HEDGED


@pytest.mark.parametrize("amount", [Decimal("0"), Decimal("-10")])
def test_expected_amount_must_be_positive(amount: Decimal) -> None:
    with pytest.raises(ValidationError):
        c.Slots(expected_amount=amount)


def test_transaction_ref_needs_at_least_one_field() -> None:
    with pytest.raises(ValidationError, match="at least one field"):
        c.TransactionRef()


def test_transaction_ref_by_date_amount_and_merchant() -> None:
    ref = c.TransactionRef(
        transaction_date=date(2026, 9, 20), amount=Decimal("12.5"), merchant="Uber"
    )
    assert ref.transaction_id is None


def test_conversation_flags_default_to_false() -> None:
    assert not any(c.ConversationFlags().model_dump().values())


# --- Model signals ----------------------------------------------------------


def test_model_signals_valid() -> None:
    signals = c.ModelSignals(
        source=c.ModelSource.KEV,
        model_version="kev-0.8b@1",
        reason_code_probs={c.ReasonCode.UNRECOGNIZED: 0.9, c.ReasonCode.DUPLICATE: 0.1},
        ambiguity=0.0,
        escalation_risk=1.0,
        manipulation=0.2,
    )
    assert signals.top_reason_code == (c.ReasonCode.UNRECOGNIZED, 0.9)


@pytest.mark.parametrize("value", [-0.01, 1.01, float("nan"), float("inf")])
@pytest.mark.parametrize("field", ["ambiguity", "escalation_risk", "manipulation"])
def test_signal_probabilities_must_be_in_unit_interval(field: str, value: float) -> None:
    with pytest.raises(ValidationError):
        c.ModelSignals.model_validate({"source": "kev", field: value})


@pytest.mark.parametrize("value", [-0.1, 1.5])
def test_reason_code_probability_must_be_in_unit_interval(value: float) -> None:
    with pytest.raises(ValidationError):
        c.ModelSignals(source=c.ModelSource.KEV, reason_code_probs={c.ReasonCode.FEE: value})


def test_reason_code_probabilities_cannot_sum_above_one() -> None:
    with pytest.raises(ValidationError, match="sum to at most 1"):
        c.ModelSignals(
            source=c.ModelSource.KEV,
            reason_code_probs={c.ReasonCode.FEE: 0.7, c.ReasonCode.DUPLICATE: 0.4},
        )


def test_reason_code_probability_keys_must_be_reason_codes() -> None:
    with pytest.raises(ValidationError):
        c.ModelSignals(source=c.ModelSource.KEV, reason_code_probs={"RC_OTHER": 0.5})  # type: ignore[dict-item]


def test_unavailable_signals_are_empty() -> None:
    signals = c.ModelSignals(source=c.ModelSource.UNAVAILABLE)
    assert signals.top_reason_code is None
    with pytest.raises(ValidationError, match="must be empty"):
        c.ModelSignals(source=c.ModelSource.UNAVAILABLE, escalation_risk=0.5)


# --- Input guard and extraction ---------------------------------------------


def test_input_guard_results() -> None:
    clean = c.InputGuardResult(flagged=False, strikes=0)
    assert not clean.escalate_security
    flagged = c.InputGuardResult(
        flagged=True, strikes=2, pattern_id="override_instructions", escalate_security=True
    )
    assert flagged.escalate_security


@pytest.mark.parametrize(
    "data",
    [
        {"flagged": True, "strikes": 1},
        {"flagged": False, "strikes": 0, "pattern_id": "x"},
        {"flagged": False, "strikes": 0, "escalate_security": True},
        {"flagged": False, "strikes": -1},
    ],
)
def test_input_guard_inconsistent_results_are_rejected(data: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        c.InputGuardResult(**data)


def test_extraction_result_defaults() -> None:
    result = c.ExtractionResult()
    assert result.slots == c.Slots()
    assert result.detected_language is None


@pytest.mark.parametrize("language", ["es", "pt", "en"])
def test_extraction_detected_language_accepts_any_iso_code(language: str) -> None:
    assert c.ExtractionResult(detected_language=language).detected_language == language


@pytest.mark.parametrize("language", ["ES", "spanish", ""])
def test_extraction_detected_language_rejects_non_codes(language: str) -> None:
    with pytest.raises(ValidationError):
        c.ExtractionResult(detected_language=language)


def test_llm_context_carries_only_minimized_data() -> None:
    context = c.LLMContext(
        customer_ref="CUS-pseudonym-7f3a",
        language=c.Language.ES,
        masked_products=["****4821"],
        transactions=[
            c.LLMTransaction(
                transaction_ref="TXN-1",
                transaction_date=date(2026, 9, 20),
                amount=Decimal("10"),
                currency="MXN",
                merchant_name="Uber",
                transaction_status="Approved",
            )
        ],
    )
    assert context.masked_products == ["****4821"]
    with pytest.raises(ValidationError):
        c.LLMContext(customer_ref="CUS-1", masked_products=["4959310341316475"])
    with pytest.raises(ValidationError):
        c.LLMContext(customer_ref="CUS-1", email="a@b.c")  # type: ignore[call-arg]


# --- Session and counters ---------------------------------------------------


def test_session_valid_and_ordering() -> None:
    assert session().customer_id == CUSTOMER
    with pytest.raises(ValidationError, match="cannot precede"):
        session(last_activity_at=NOW - timedelta(hours=2))


def test_session_requires_timezone_aware_datetimes() -> None:
    with pytest.raises(ValidationError):
        session(issued_at=datetime(2026, 9, 28, 11, 0))


def test_counters_reject_negative_values() -> None:
    assert c.ConversationCounters().total_clarifications == 0
    assert c.ConversationCounters().authentication_attempts == 0
    with pytest.raises(ValidationError):
        c.ConversationCounters(injection_strikes=-1)
    with pytest.raises(ValidationError):
        c.ConversationCounters(authentication_attempts=-1)
    with pytest.raises(ValidationError):
        c.ConversationCounters(clarifications_by_slot={c.ClarifyTarget.REASON_CODE: -1})


# --- Tool results -----------------------------------------------------------


def test_tool_result_success() -> None:
    assert tool_result().verified


def test_tool_result_failed_and_access_denied() -> None:
    failed = tool_result(
        status=c.ToolStatus.FAILED, verified=False, record_id=None, attempts=3, error="timeout"
    )
    assert failed.attempts == 3
    denied = tool_result(
        action=c.ActionId.BLOCK_CARD,
        status=c.ToolStatus.ACCESS_DENIED,
        verified=False,
        record_id=None,
        idempotency_key=None,
    )
    assert denied.status is c.ToolStatus.ACCESS_DENIED


def test_transfer_to_human_needs_no_record_id() -> None:
    result = tool_result(action=c.ActionId.TRANSFER_TO_HUMAN, record_id=None, idempotency_key=None)
    assert result.status is c.ToolStatus.SUCCESS


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"verified": False}, "verified if and only if"),
        ({"status": c.ToolStatus.FAILED, "error": "x"}, "verified if and only if"),
        ({"record_id": None}, "record it wrote"),
        ({"status": c.ToolStatus.FAILED, "verified": False}, "describe its error"),
        ({"idempotency_key": None}, "idempotency key"),
        ({"attempts": 0}, "greater than or equal to 1"),
    ],
)
def test_tool_result_invariants(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        tool_result(**overrides)


# --- Policy request ---------------------------------------------------------


def test_minimal_policy_request_without_session() -> None:
    request = c.PolicyRequest(now=NOW, as_of=AS_OF, conversation_id="CONV-1")
    assert request.session is None
    assert request.signals.source is c.ModelSource.UNAVAILABLE
    assert request.slots == c.Slots()


def test_full_policy_request() -> None:
    request = c.PolicyRequest(
        now=NOW,
        as_of=AS_OF,
        conversation_id="CONV-1",
        detected_language="pt",
        session=session(),
        slots=c.Slots(reason_code=c.ReasonCode.UNRECOGNIZED),
        customer=c.CustomerRecord(customer_id=CUSTOMER, customer_status="Active"),
        transaction_candidates=[transaction()],
        product=product(),
        duplicate_candidates=[transaction(transaction_id="TXN-2")],
        fee_candidates=[transaction(transaction_id="TXN-3", transaction_type="Adjustment")],
        open_cases=[case()],
        dispute_history=c.DisputeHistory(disputed_usd_last_30d=Decimal("0"), cases_last_90d=0),
        tool_results=[tool_result()],
        input_guard=c.InputGuardResult(flagged=False, strikes=0),
    )
    assert request.transaction_candidates[0].transaction_id == "TXN-1"


def test_records_without_session_are_rejected() -> None:
    with pytest.raises(ValidationError, match="without a session"):
        c.PolicyRequest(
            now=NOW, as_of=AS_OF, conversation_id="CONV-1", transaction_candidates=[transaction()]
        )


@pytest.mark.parametrize(
    "field",
    ["customer", "transaction_candidates", "product", "duplicate_candidates", "open_cases"],
)
def test_records_of_another_customer_are_rejected(field: str) -> None:
    values: dict[str, Any] = {
        "customer": c.CustomerRecord(customer_id=OTHER, customer_status="Active"),
        "transaction_candidates": [transaction(customer_id=OTHER)],
        "product": product(customer_id=OTHER),
        "duplicate_candidates": [transaction(customer_id=OTHER)],
        "open_cases": [case(customer_id=OTHER)],
    }
    with pytest.raises(ValidationError, match="GATE-04"):
        c.PolicyRequest(
            now=NOW,
            as_of=AS_OF,
            conversation_id="CONV-1",
            session=session(),
            **{field: values[field]},
        )


@pytest.mark.parametrize(
    "clocks",
    [
        {"now": datetime(2026, 9, 28), "as_of": AS_OF},
        {"now": NOW, "as_of": datetime(2026, 6, 18, 6, 0, tzinfo=UTC)},
        {"now": NOW},
        {"as_of": AS_OF},
    ],
)
def test_policy_request_requires_both_clocks_with_their_kind(clocks: dict[str, datetime]) -> None:
    with pytest.raises(ValidationError):
        c.PolicyRequest.model_validate({"conversation_id": "CONV-1", **clocks})


def test_business_date_is_independent_of_real_time() -> None:
    # Policy §15: the dataset ends on 2026-06-17 while sessions run in real time.
    request = c.PolicyRequest(now=NOW, as_of=AS_OF, conversation_id="CONV-1")
    assert request.as_of.tzinfo is None
    assert request.now.tzinfo is not None


# --- Policy decision --------------------------------------------------------


def test_resolve_decision_valid() -> None:
    result = decision()
    assert result.outcome is c.Outcome.RESOLVE
    assert result.gates_evaluated[0].passed


def test_escalate_decision_valid() -> None:
    result = decision(
        outcome=c.Outcome.ESCALATE,
        triggered_rules=["ESC-03"],
        queue=c.Queue.FRAUD,
        priority=c.Priority.HIGH,
        authorized_actions=[c.ActionId.BLOCK_CARD, c.ActionId.TRANSFER_TO_HUMAN],
        tier=c.Tier.T3,
        provisional_credit_flag=None,
    )
    assert result.queue is c.Queue.FRAUD


def test_clarify_inform_and_refuse_decisions_valid() -> None:
    base: dict[str, Any] = {
        "authorized_actions": [],
        "tier": None,
        "provisional_credit_flag": None,
    }
    clarify = decision(
        outcome=c.Outcome.CLARIFY, clarify_target=c.ClarifyTarget.TRANSACTION_REF, **base
    )
    inform = decision(
        outcome=c.Outcome.INFORM, inform_reason=c.InformReason.TRANSACTION_PENDING, **base
    )
    refuse = decision(outcome=c.Outcome.REFUSE, triggered_rules=["GATE-04"], **base)
    assert (clarify.outcome, inform.outcome, refuse.outcome) == (
        c.Outcome.CLARIFY,
        c.Outcome.INFORM,
        c.Outcome.REFUSE,
    )


def test_t2_resolve_requires_review_flag() -> None:
    assert (
        decision(
            tier=c.Tier.T2, provisional_credit_flag=c.ProvisionalCreditFlag.REQUIRES_REVIEW
        ).tier
        is c.Tier.T2
    )


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"outcome": c.Outcome.ESCALATE, "triggered_rules": ["ESC-01"]}, "queue and priority"),
        ({"queue": c.Queue.DISPUTES, "priority": c.Priority.NORMAL}, "queue and priority"),
        (
            {
                "outcome": c.Outcome.ESCALATE,
                "queue": c.Queue.DISPUTES,
                "priority": c.Priority.NORMAL,
                "authorized_actions": [],
                "triggered_rules": ["GATE-03"],
            },
            "must name the escalation rule",
        ),
        ({"clarify_target": c.ClarifyTarget.REASON_CODE}, "clarify_target"),
        ({"inform_reason": c.InformReason.OUTSIDE_WINDOW}, "inform_reason"),
        (
            {"tier": c.Tier.T3, "provisional_credit_flag": None},
            "RESOLVE requires tier T1 or T2",
        ),
        ({"tier": None, "provisional_credit_flag": None}, "RESOLVE requires tier T1 or T2"),
        (
            {"outcome": c.Outcome.INFORM, "inform_reason": c.InformReason.TRANSACTION_PENDING},
            "only be authorized with a RESOLVE",
        ),
        ({"transaction_id": None}, "needs the transaction"),
        ({"reason_code": None}, "needs the transaction"),
        ({"authorized_actions": [c.ActionId.CREATE_CASE]}, "ACT-04"),
        (
            {"authorized_actions": [], "tier": c.Tier.T1},
            None,
        ),
        (
            {"provisional_credit_flag": c.ProvisionalCreditFlag.REQUIRES_REVIEW},
            "does not match the tier",
        ),
        (
            {
                "outcome": c.Outcome.REFUSE,
                "authorized_actions": [c.ActionId.TRANSFER_TO_HUMAN],
                "tier": None,
                "provisional_credit_flag": None,
            },
            "authorizes no action",
        ),
        ({"triggered_rules": ["ACT-02"]}, "should match pattern"),
        ({"gates_evaluated": [{"gate_id": "ESC-01", "passed": True}]}, "should match pattern"),
    ],
)
def test_decision_invariants(overrides: dict[str, Any], message: str | None) -> None:
    if message is None:
        # A RESOLVE without authorized actions is structurally valid (e.g. awaiting read-back).
        decision(**overrides)
        return
    with pytest.raises(ValidationError, match=message):
        decision(**overrides)


def test_credit_flag_without_tier_is_rejected() -> None:
    with pytest.raises(ValidationError, match="does not match the tier"):
        decision(
            outcome=c.Outcome.INFORM,
            inform_reason=c.InformReason.OUTSIDE_WINDOW,
            authorized_actions=[],
            tier=None,
        )


# --- Handoff packet ---------------------------------------------------------


def test_policy_section_13_example_validates() -> None:
    packet = c.HandoffPacket.model_validate(POLICY_HANDOFF_EXAMPLE)
    assert packet.queue is c.Queue.FRAUD
    assert packet.verified_facts[0].source == "transactions"
    assert packet.draft_case is not None
    assert packet.draft_case.amount_usd == Decimal("68.4")


def test_handoff_round_trips_through_json() -> None:
    packet = c.HandoffPacket.model_validate(POLICY_HANDOFF_EXAMPLE)
    restored = c.HandoffPacket.model_validate_json(packet.model_dump_json())
    assert restored == packet
    assert json.loads(packet.model_dump_json())["language"] == "pt"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("handoff_id", "HO-1"),
        ("language", "en"),
        ("queue", "sales"),
        ("triggered_rules", []),
        ("transcript_ref", ""),
        ("created_at", "2026-09-25T22:45:00"),
    ],
)
def test_handoff_rejects_invalid_values(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        c.HandoffPacket.model_validate(POLICY_HANDOFF_EXAMPLE | {field: value})


@pytest.mark.parametrize(
    "field", ["transcript", "document_number", "date_of_birth", "address", "email", "full_name"]
)
def test_handoff_rejects_raw_transcript_and_personal_data(field: str) -> None:
    with pytest.raises(ValidationError, match="Extra inputs"):
        c.HandoffPacket.model_validate(POLICY_HANDOFF_EXAMPLE | {field: "x"})


def test_handoff_verified_fact_without_record_id_is_rejected() -> None:
    bad = POLICY_HANDOFF_EXAMPLE | {
        "verified_facts": [{"fact": "Card is Active", "source": "products"}]
    }
    with pytest.raises(ValidationError, match="record_id"):
        c.HandoffPacket.model_validate(bad)


@pytest.mark.parametrize("field", ["handoff_id", "queue", "priority", "customer_ref", "auth"])
def test_handoff_required_fields(field: str) -> None:
    data = dict(POLICY_HANDOFF_EXAMPLE)
    del data[field]
    with pytest.raises(ValidationError, match=field):
        c.HandoffPacket.model_validate(data)


def test_failed_action_can_be_listed() -> None:
    action = c.ActionTaken(
        action=c.ActionId.CREATE_CASE, result=c.ToolStatus.FAILED, verified=False
    )
    assert action.result is c.ToolStatus.FAILED


# --- Trace record -----------------------------------------------------------


def test_minimal_trace_records_absent_stages_as_none() -> None:
    trace = c.TraceRecord(
        trace_id="TR-1",
        conversation_id="CONV-1",
        turn_index=0,
        created_at=NOW,
        policy_version="0.1.0",
    )
    dumped = trace.model_dump()
    for field in ("session_id", "input_guard", "signals", "outcome", "handoff_id", "error"):
        assert field in dumped
        assert dumped[field] is None


def test_full_trace_record() -> None:
    trace = c.TraceRecord(
        trace_id="TR-1",
        conversation_id="CONV-1",
        session_id="SES-1",
        turn_index=3,
        created_at=NOW,
        language=c.Language.ES,
        input_guard=c.InputGuardResult(flagged=False, strikes=0),
        model_calls=[
            c.ModelCall(
                provider="openai",
                model="gpt-6-luna",
                prompt_version="extract@1",
                purpose="extract_slots",
                input_tokens=500,
                output_tokens=80,
                latency_ms=812.5,
                success=True,
            )
        ],
        signals=c.ModelSignals(source=c.ModelSource.LLM_FALLBACK),
        decisions=[decision()],
        tool_calls=[tool_result()],
        outcome=c.Outcome.RESOLVE,
        stage_latencies_ms={"policy": 1.2},
        total_latency_ms=1500.0,
        estimated_cost_usd=Decimal("0.0021"),
        policy_version="0.1.0",
    )
    assert c.TraceRecord.model_validate_json(trace.model_dump_json()) == trace


@pytest.mark.parametrize(
    "overrides",
    [
        {"turn_index": -1},
        {"total_latency_ms": -1.0},
        {"total_latency_ms": float("inf")},
        {"stage_latencies_ms": {"policy": -1.0}},
        {"estimated_cost_usd": Decimal("-0.01")},
    ],
)
def test_trace_rejects_invalid_values(overrides: dict[str, Any]) -> None:
    data: dict[str, Any] = {
        "trace_id": "TR-1",
        "conversation_id": "CONV-1",
        "turn_index": 0,
        "created_at": NOW,
        "policy_version": "0.1.0",
    }
    with pytest.raises(ValidationError):
        c.TraceRecord(**(data | overrides))


def test_model_call_rejects_negative_tokens() -> None:
    with pytest.raises(ValidationError):
        c.ModelCall(
            provider="kev",
            model="kev",
            purpose="signals",
            latency_ms=1,
            success=True,
            input_tokens=-1,
        )


def test_access_denied_error_is_an_exception() -> None:
    with pytest.raises(c.AccessDeniedError):
        raise c.AccessDeniedError("access_denied")
