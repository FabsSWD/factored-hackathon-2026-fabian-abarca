"""M10: the policy §13 packet, built from real Policy Engine decisions."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.contracts import (
    ActionId,
    AuthStatus,
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    HandoffPacket,
    InputGuardResult,
    Language,
    ModelSignals,
    ModelSource,
    PolicyDecision,
    PolicyRequest,
    Priority,
    Queue,
    ReasonCode,
    ToolResult,
    ToolStatus,
)
from app.handoff import PolicyHandoffBuilder
from app.interfaces import HandoffBuilder
from app.pseudonym import UNAUTHENTICATED_REF, customer_ref
from tests.policy.conftest import (
    CONFIG,
    CUSTOMER_ID,
    case,
    counters,
    evaluate,
    product,
    request,
    session,
    slots,
    txn,
    unauthenticated,
)

KEY = "test-pseudonym-key"
CREATED = datetime(2026, 10, 1, 12, 30, tzinfo=UTC)
POLICY = Path(__file__).resolve().parents[2] / "docs" / "dispute-policy.md"
FORBIDDEN_KEYS = {
    "customer_id",
    "document_number",
    "document_type",
    "document_hash",
    "date_of_birth",
    "age_band",
    "address",
    "city",
    "postal_code",
    "phone",
    "mobile_phone",
    "landline_phone",
    "email",
    "first_name",
    "last_name",
    "full_name",
    "transcript",
    "messages",
}


def builder() -> PolicyHandoffBuilder:
    return PolicyHandoffBuilder(
        CONFIG.parameters, KEY, lambda at: f"HO-{at:%Y%m%d}-000001", clock=lambda: CREATED
    )


def build(
    req: PolicyRequest, decision: PolicyDecision | None = None, **values: Any
) -> HandoffPacket:
    arguments: dict[str, Any] = {
        "request": req,
        "decision": decision or evaluate(req),
        "language": Language.ES,
        "customer_claims": [],
        "actions_taken": [],
        "open_questions": [],
        "transcript_ref": "CONV-1",
    }
    arguments.update(values)
    return builder().build(**arguments)


def test_implements_the_protocol() -> None:
    assert isinstance(builder(), HandoffBuilder)


@pytest.mark.parametrize(
    "req",
    [
        request(),
        request(ownership_violation=True),
        request(transaction_candidates=[txn(status="Pending")]),
    ],
    ids=["clarify", "refuse", "inform"],
)
def test_only_escalate_outcomes_get_a_packet(req: PolicyRequest) -> None:
    with pytest.raises(ValueError, match="only for ESCALATE"):
        build(req)


# --- Queue and priority routes (policy §7) ----------------------------------------------------


CONFIRMED = slots(confirmation=Confirmation.CONFIRMED)
GUARD = InputGuardResult(flagged=True, strikes=2, pattern_id="override", escalate_security=True)
ROUTES = [
    (
        "ESC-01",
        request(slots=CONFIRMED, transaction_candidates=[txn(amount="2000", amount_usd="1500")]),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    (
        "ESC-02",
        request(slots=CONFIRMED, cases=[case(f"C-{i}", amount_usd="10") for i in range(3)]),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    ("ESC-03", request(flags={"account_takeover_reported": True}), Queue.FRAUD, Priority.HIGH),
    (
        "ESC-04",
        request(slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)]),
        Queue.FRAUD,
        Priority.NORMAL,
    ),
    ("ESC-05", request(flags={"human_requested": True}), Queue.DISPUTES, Priority.NORMAL),
    ("ESC-06", request(flags={"legal_or_vulnerability": True}), Queue.DISPUTES, Priority.HIGH),
    (
        "ESC-07",
        request(transaction_candidates=[txn(when=datetime(2026, 3, 1, 12))]),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    ("ESC-08", request(products=[product(status="Closed")]), Queue.DISPUTES, Priority.NORMAL),
    (
        "ESC-09",
        request(counters=counters(unresolved_contradiction=True)),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    (
        "ESC-10",
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
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    (
        "ESC-11",
        request(signals=ModelSignals(source=ModelSource.UNAVAILABLE)),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    (
        "ESC-12",
        unauthenticated(detected_language="en", counters=counters(language_clarifications=1)),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
    ("ESC-13", request(input_guard=GUARD), Queue.SECURITY_REVIEW, Priority.NORMAL),
    (
        "ESC-14",
        request(
            transaction_candidates=[txn(transaction_type="Transfer")],
            slots=slots(reason_code=ReasonCode.UNRECOGNIZED),
        ),
        Queue.FRAUD,
        Priority.NORMAL,
    ),
    (
        "ESC-14",
        request(
            transaction_candidates=[txn(transaction_type="Withdrawal")],
            slots=slots(reason_code=ReasonCode.DUPLICATE),
        ),
        Queue.DISPUTES,
        Priority.NORMAL,
    ),
]


@pytest.mark.parametrize(("rule", "req", "queue", "priority"), ROUTES)
def test_queue_and_priority_routes(
    rule: str, req: PolicyRequest, queue: Queue, priority: Priority
) -> None:
    packet = build(req)
    assert rule in packet.triggered_rules
    assert packet.queue is queue
    assert packet.priority is priority


def test_several_triggers_keep_every_rule_and_the_strictest_route() -> None:
    req = request(
        flags={"human_requested": True, "account_takeover_reported": True}, input_guard=GUARD
    )
    packet = build(req)
    assert packet.triggered_rules == ["ESC-03", "ESC-05", "ESC-13"]
    assert (packet.queue, packet.priority) == (Queue.SECURITY_REVIEW, Priority.HIGH)


# --- Facts and claims (DATA-03, DATA-04) ------------------------------------------------------


def test_every_fact_has_source_and_record_id_from_the_records() -> None:
    req = request(
        slots=slots(reason_code=ReasonCode.UNRECOGNIZED),
        flags={"human_requested": True},
    )
    packet = build(req)
    known = {
        "customers": {customer_ref(CUSTOMER_ID, KEY)},
        "transactions": {t.transaction_id for t in req.transaction_candidates},
        "products": {p.product_id for p in req.products},
        "cases": {c.case_id for c in req.cases},
    }
    assert packet.verified_facts
    for fact in packet.verified_facts:
        assert fact.source in known and fact.record_id in known[fact.source]
    texts = [f.fact for f in packet.verified_facts]
    assert "Customer status is Active" in texts
    assert (
        "Purchase of USD 50.00 at Cafe Sintetico on 2026-06-10, status Approved, "
        "7 days before the business date"
    ) in texts
    assert "Card ending 4821 is Active" in texts


def test_customer_claims_never_become_facts() -> None:
    claims = ["I did not make this purchase", "My phone was stolen yesterday"]
    packet = build(request(flags={"human_requested": True}), customer_claims=claims)
    assert packet.customer_claims[:2] == claims
    fact_texts = " ".join(f.fact for f in packet.verified_facts)
    assert not any(claim in fact_texts for claim in claims)


def test_duplicate_pair_and_card_facts() -> None:
    first = txn("TXN-1", when=datetime(2026, 6, 10, 9))
    second = txn("TXN-2", when=datetime(2026, 6, 10, 12))
    req = request(
        transaction_candidates=[first, second],
        products=[product(product_type="Cuenta Corriente", last4="1111")],
        slots=slots(reason_code=ReasonCode.DUPLICATE, transaction_ref={"transaction_id": "TXN-2"}),
        flags={"human_requested": True},
    )
    packet = build(req)
    texts = [f.fact for f in packet.verified_facts]
    assert any(t.startswith("Possible duplicate: Purchase") for t in texts)
    assert "Cuenta Corriente ending 1111 is Active" in texts


def test_card_of_an_esc03_without_transaction_is_a_fact() -> None:
    req = request(flags={"account_takeover_reported": True}, slots=slots(transaction_ref=None))
    packet = build(req)
    assert [f.record_id for f in packet.verified_facts if f.source == "products"] == ["PRD-1"]
    assert packet.draft_case is None


def test_fraud_score_and_previous_cases_explain_their_triggers() -> None:
    previous = [case(f"C-{i}", amount_usd="10") for i in range(3)]
    draft = case("C-D", status=CaseStatus.DRAFT)
    req = request(
        slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)], cases=[*previous, draft]
    )
    packet = build(req)
    texts = [f.fact for f in packet.verified_facts]
    assert "Fraud score 91.5 on the disputed transaction" in texts
    assert sum(t.startswith("Previous case C-") for t in texts) == 3
    assert not any("C-D" in t for t in texts)


# --- Actions, draft case, signals ------------------------------------------------------------


def test_failed_action_appears_in_actions_taken() -> None:
    failed = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.FAILED,
        verified=False,
        attempts=3,
        error="timeout",
    )
    ok = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.SUCCESS,
        verified=True,
        attempts=1,
        record_id="PRD-1",
        detail="Card ending 4821 blocked",
    )
    req = request(tool_results=[failed])
    packet = build(req, actions_taken=[ok, failed])
    assert [(a.result, a.verified, a.detail) for a in packet.actions_taken] == [
        (ToolStatus.SUCCESS, True, "Card ending 4821 blocked"),
        (ToolStatus.FAILED, False, "timeout"),
    ]
    assert any("ACT-03 could not be verified (timeout)" in q for q in packet.open_questions)


def test_draft_case_from_the_decision() -> None:
    packet = build(
        request(slots=CONFIRMED, transaction_candidates=[txn(amount="2000", amount_usd="1500")])
    )
    assert packet.draft_case is not None
    assert packet.draft_case.transaction_ref == "TXN-1"
    assert packet.draft_case.tier == "T3"
    assert packet.draft_case.provisional_credit_flag is None


def test_model_signals_are_informative_copies() -> None:
    kev = ModelSignals(
        source=ModelSource.KEV,
        model_version="kev-0.8b",
        reason_code_probs={ReasonCode.UNRECOGNIZED: 0.9},
        reason_code_other=0.1,
        escalation_risk=0.4,
    )
    packet = build(request(signals=kev, flags={"human_requested": True}))
    assert packet.model_signals.reason_code_probs == {ReasonCode.UNRECOGNIZED: 0.9}
    assert packet.model_signals.escalation_risk == 0.4
    assert packet.model_signals.model_version == "kev-0.8b"
    unavailable = build(request(signals=ModelSignals(source=ModelSource.UNAVAILABLE)))
    assert unavailable.model_signals.reason_code_probs == {}
    fallback = build(request(flags={"human_requested": True}))
    assert fallback.model_signals.source is ModelSource.LLM_FALLBACK
    assert fallback.model_signals.model_version is None


# --- Authentication -----------------------------------------------------------------------------


def test_unauthenticated_packet_has_no_account_data() -> None:
    packet = build(unauthenticated(flags={"human_requested": True}))
    assert packet.auth.status is AuthStatus.UNAUTHENTICATED
    assert packet.customer_ref == UNAUTHENTICATED_REF
    assert packet.verified_facts == [] and packet.draft_case is None
    assert "Verify the customer's identity (not verified)." in packet.open_questions


def test_expired_session_is_reported_and_withholds_account_data() -> None:
    packet = build(request(session=session(age_min=61), flags={"human_requested": True}))
    assert packet.auth.status is AuthStatus.EXPIRED
    assert packet.auth.method == "test_otp" and packet.auth.session_age_min == 61.0
    assert packet.verified_facts == []
    assert "Verify the customer's identity (the session expired)." in packet.open_questions


def test_authenticated_session_age() -> None:
    packet = build(request(flags={"human_requested": True}))
    assert packet.auth.status is AuthStatus.AUTHENTICATED
    assert packet.auth.session_age_min == 5.0


# --- Open questions -----------------------------------------------------------------------------


def test_open_questions_empty_only_when_nothing_is_pending() -> None:
    assert build(request(flags={"human_requested": True})).open_questions == []


@pytest.mark.parametrize(
    ("req", "question"),
    [
        (
            request(
                flags={"human_requested": True},
                transaction_candidates=[txn(fraud_score=None)],
                slots=CONFIRMED,
            ),
            "no fraud score",
        ),
        (
            request(flags={"human_requested": True}, slots=slots(transaction_ref=None)),
            "Which transaction",
        ),
        (
            request(flags={"human_requested": True}, slots=slots(reason_code=None)),
            "What is the reason",
        ),
        (
            request(
                flags={"account_takeover_reported": True},
                slots=slots(transaction_ref=None),
                products=[product("P1"), product("P2", last4="2222")],
            ),
            "several active cards",
        ),
        (
            request(
                transaction_candidates=[txn(transaction_type="Transfer", product_id="PRD-A")],
                products=[product("PRD-A", product_type="Cuenta Ahorro")],
                slots=slots(reason_code=ReasonCode.UNRECOGNIZED),
            ),
            "not a card",
        ),
    ],
)
def test_pending_items_become_open_questions(req: PolicyRequest, question: str) -> None:
    assert any(question in q for q in build(req).open_questions)


def test_given_questions_come_first_and_are_deduplicated() -> None:
    packet = build(
        request(flags={"human_requested": True}, slots=slots(reason_code=None)),
        open_questions=["Ask about other charges.", "What is the reason for the dispute?", " "],
    )
    assert packet.open_questions == [
        "Ask about other charges.",
        "What is the reason for the dispute?",
    ]


def test_unknown_note_still_becomes_a_question() -> None:
    decision = evaluate(request(flags={"human_requested": True}))
    decision = decision.model_copy(update={"notes": ["something_new"]})
    assert build(request(), decision).open_questions == ["Review: something_new."]


# --- Serialization and the policy example -----------------------------------------------------


def _keys(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for item in value for k in _keys(item)}
    return set()


def _policy_example() -> dict[str, Any]:
    text = POLICY.read_text(encoding="utf-8")
    section = text[text.index("## 13. Handoff packet") :]
    block = re.search(r"```json\n(.*?)```", section, re.DOTALL)
    assert block is not None
    data: dict[str, Any] = json.loads(block.group(1))
    return data


def full_packet() -> HandoffPacket:
    req = request(
        slots=slots(
            reason_code=ReasonCode.UNRECOGNIZED, card_in_possession=True, shared_credentials=False
        ),
        flags={"account_takeover_reported": True},
        signals=ModelSignals(
            source=ModelSource.KEV,
            model_version="kev",
            reason_code_probs={ReasonCode.UNRECOGNIZED: 0.94},
            reason_code_other=0.06,
            escalation_risk=0.81,
        ),
    )
    blocked = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.SUCCESS,
        verified=True,
        attempts=1,
        record_id="PRD-1",
        detail="Card ending 4821 blocked",
        completed_at=CREATED - timedelta(seconds=30),
    )
    return build(
        req,
        customer_claims=["No hice esa compra"],
        actions_taken=[blocked],
        open_questions=["Were other charges made after the phone was stolen?"],
    )


def test_policy_example_is_a_valid_packet() -> None:
    HandoffPacket.model_validate(_policy_example())


def test_packet_has_the_shape_of_the_policy_example() -> None:
    produced = json.loads(full_packet().model_dump_json())
    example = _policy_example()
    assert set(produced) == set(example)
    for key in ("auth", "draft_case", "model_signals"):
        assert set(produced[key]) == set(example[key]), key
    assert set(produced["verified_facts"][0]) == set(example["verified_facts"][0])
    assert set(produced["actions_taken"][0]) == set(example["actions_taken"][0])
    reason, example_reason = produced["escalation_reasons"][0], example["escalation_reasons"][0]
    assert set(reason) == set(example_reason)
    assert set(reason["evidence"][0]) == set(example_reason["evidence"][0])
    assert re.fullmatch(r"HO-\d{8}-\d{6,}", produced["handoff_id"])


def test_packet_serializes_without_prohibited_fields() -> None:
    packet = full_packet()
    text = packet.model_dump_json()
    assert HandoffPacket.model_validate_json(text) == packet
    assert _keys(json.loads(text)) & FORBIDDEN_KEYS == set()
    assert CUSTOMER_ID not in text  # pseudonymous reference only
    assert packet.customer_ref == customer_ref(CUSTOMER_ID, KEY)
    assert packet.transcript_ref == "CONV-1"


def test_handoff_id_and_created_at_come_from_the_injected_sources() -> None:
    packet = full_packet()
    assert packet.handoff_id == "HO-20261001-000001"
    assert packet.created_at == CREATED
    assert packet.policy_version == CONFIG.policy_version


def test_default_clock_is_utc() -> None:
    packet = PolicyHandoffBuilder(CONFIG.parameters, KEY, lambda at: "HO-20261001-000002").build(
        request=request(flags={"human_requested": True}),
        decision=evaluate(request(flags={"human_requested": True})),
        language=Language.PT,
        customer_claims=[],
        actions_taken=[],
        open_questions=[],
        transcript_ref="CONV-2",
    )
    assert packet.created_at.tzinfo is not None
    assert datetime.now(UTC) - packet.created_at < timedelta(minutes=1)
    assert packet.language is Language.PT


# --- Pseudonyms -----------------------------------------------------------------------------


def test_customer_ref_is_stable_keyed_and_opaque() -> None:
    ref = customer_ref("CLI-ALPHA0000001", KEY)
    assert re.fullmatch(r"CUS-[0-9a-f]{16}", ref)
    assert ref == customer_ref("CLI-ALPHA0000001", KEY)
    assert ref != customer_ref("CLI-ALPHA0000001", "another-key")
    assert ref != customer_ref("CLI-BETA00000002", KEY)
    assert "ALPHA" not in ref
    with pytest.raises(ValueError, match="key"):
        customer_ref("CLI-ALPHA0000001", "")


def test_clarify_target_is_irrelevant_to_packets() -> None:
    # A CLARIFY that ESC-09 replaced still builds a packet; the target is not part of §13.
    req = request(
        slots=slots(expected_amount=None),
        counters=counters(clarifications_by_slot={ClarifyTarget.EXPECTED_AMOUNT: 2}),
    )
    assert build(req).triggered_rules == ["ESC-09"]


def test_missing_records_give_no_fact_and_an_open_question() -> None:
    no_customer = build(request(customer=None))
    assert all(f.source != "customers" for f in no_customer.verified_facts)
    assert any("customer record could not be read" in q for q in no_customer.open_questions)
    no_product = build(request(products=[product(product_id="PRD-OTHER")]))
    assert all(f.source != "products" for f in no_product.verified_facts)
    assert any("product record could not be read" in q for q in no_product.open_questions)


# --- Review of the packet (M10 review) ---------------------------------------------------------


def test_no_fact_says_a_card_blocked_by_act03_is_active() -> None:
    packet = full_packet()
    card_facts = [f.fact for f in packet.verified_facts if f.record_id == "PRD-1"]
    assert card_facts == ["Card ending 4821 is Blocked (blocked by ACT-03 at 2026-10-01 12:29 UTC)"]
    assert not any("is Active" in f.fact for f in packet.verified_facts if f.source == "products")
    assert packet.actions_taken[0].at == CREATED - timedelta(seconds=30)


def test_block_without_time_and_failed_block_keep_the_record_status() -> None:
    timeless = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.SUCCESS,
        verified=True,
        attempts=1,
        record_id="PRD-1",
    )
    failed = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.FAILED,
        verified=False,
        attempts=3,
        error="timeout",
    )
    req = request(flags={"account_takeover_reported": True}, tool_results=[failed])
    assert "Card ending 4821 is Blocked (blocked by ACT-03)" in [
        f.fact
        for f in build(
            request(flags={"account_takeover_reported": True}), actions_taken=[timeless]
        ).verified_facts
    ]
    assert "Card ending 4821 is Active" in [
        f.fact for f in build(req, actions_taken=[failed]).verified_facts
    ]


@pytest.mark.parametrize(("rule", "req", "queue", "priority"), ROUTES)
def test_every_triggered_rule_has_a_reason_with_evidence(
    rule: str, req: PolicyRequest, queue: Queue, priority: Priority
) -> None:
    packet = build(req)
    assert [r.rule_id for r in packet.escalation_reasons] == packet.triggered_rules
    for reason in packet.escalation_reasons:
        assert reason.description
        assert reason.evidence
        for item in reason.evidence:
            assert item.origin and item.value
            if item.kind == "record":
                assert item.source in {"customers", "products", "transactions", "cases"}


@pytest.mark.parametrize(
    ("req", "rule", "kind", "name"),
    [
        (
            request(flags={"account_takeover_reported": True}),
            "ESC-03",
            "flag",
            "account_takeover_reported",
        ),
        (
            request(slots=slots(reason_code=ReasonCode.UNRECOGNIZED, shared_credentials=True)),
            "ESC-03",
            "slot",
            "shared_credentials",
        ),
        (
            request(counters=counters(unrecognized_transactions=3)),
            "ESC-03",
            "counter",
            "unrecognized_transactions",
        ),
        (
            request(slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)]),
            "ESC-04",
            "record",
            "fraud_score",
        ),
        (request(flags={"human_requested": True}), "ESC-05", "flag", "human_requested"),
        (
            request(transaction_candidates=[txn(when=datetime(2026, 3, 1, 12))]),
            "ESC-07",
            "record",
            "transaction_date",
        ),
        (request(input_guard=GUARD), "ESC-13", "input_guard", "pattern_id"),
    ],
)
def test_evidence_names_its_origin(req: PolicyRequest, rule: str, kind: str, name: str) -> None:
    reason = next(r for r in build(req).escalation_reasons if r.rule_id == rule)
    assert any(e.kind == kind and e.name == name for e in reason.evidence)


def test_customer_evidence_uses_the_pseudonymous_reference() -> None:
    from tests.policy.conftest import customer

    packet = build(request(customer=customer("Suspended")))
    (reason,) = packet.escalation_reasons
    assert reason.rule_id == "ESC-08"
    assert reason.evidence[0].record_id == customer_ref(CUSTOMER_ID, KEY)
    assert reason.evidence[0].value == "Suspended"
    assert CUSTOMER_ID not in packet.model_dump_json()


def test_summary_is_specific_and_templated() -> None:
    assert full_packet().request_summary == (
        "Unrecognized charge of USD 50.00 at Cafe Sintetico (2026-06-10); "
        "account takeover indicators (ESC-03); card ****4821 blocked."
    )
    no_txn = build(request(flags={"human_requested": True}, slots=slots(transaction_ref=None)))
    assert no_txn.request_summary == (
        "Incorrect amount; no transaction identified; human requested (ESC-05)."
    )
    no_reason = build(request(flags={"human_requested": True}, slots=slots(reason_code=None)))
    assert no_reason.request_summary.startswith("Dispute about a Purchase of USD 50.00")
    contact = build(
        request(
            flags={"human_requested": True}, slots=slots(transaction_ref=None, reason_code=None)
        )
    )
    assert contact.request_summary.startswith("Customer contact; no transaction identified")
    anonymous = build(unauthenticated(flags={"human_requested": True}))
    assert anonymous.request_summary == "Customer not authenticated; human requested (ESC-05)."


def test_claims_are_only_what_the_customer_said() -> None:
    req = request(
        slots=slots(
            reason_code=ReasonCode.UNRECOGNIZED, card_in_possession=False, shared_credentials=True
        ),
        flags={
            "human_requested": True,
            "legal_or_vulnerability": True,
            "account_takeover_reported": True,
        },
    )
    said = ["Me robaron el teléfono", "Le di el código a alguien que llamó"]
    packet = build(req, customer_claims=said)
    assert packet.customer_claims == said
    assert build(req).customer_claims == []  # nothing is written for the customer


def test_no_claim_is_fixed_system_text() -> None:
    from app.handoff import builder
    from app.handoff.examples import example_packets

    system_text = {
        text
        for name, value in vars(builder).items()
        if name.isupper() and isinstance(value, dict)
        for text in value.values()
        if isinstance(text, str)
    }
    for packet in [full_packet(), *example_packets().values()]:
        assert not set(packet.customer_claims) & system_text
    assert not hasattr(builder, "DERIVED_CLAIMS")


def test_evidence_points_to_the_claims_behind_it() -> None:
    req = request(
        slots=slots(reason_code=ReasonCode.UNRECOGNIZED, shared_credentials=True),
        flags={"account_takeover_reported": True},
    )
    said = ["Me robaron el teléfono ayer", "Le di el código a quien me llamó"]
    packet = build(
        req,
        customer_claims=said,
        evidence_claims={
            "account_takeover_reported": [said[0]],
            "shared_credentials": [said[1]],
        },
    )
    (reason,) = packet.escalation_reasons
    by_name = {e.name: e.claims for e in reason.evidence}
    assert by_name == {"account_takeover_reported": [said[0]], "shared_credentials": [said[1]]}


def test_evidence_links_only_to_slots_and_flags() -> None:
    req = request(slots=CONFIRMED, transaction_candidates=[txn(fraud_score=91.5)])
    packet = build(req, customer_claims=["x"], evidence_claims={"fraud_score": ["x"]})
    assert all(e.claims == [] for r in packet.escalation_reasons for e in r.evidence)


def test_evidence_claims_must_be_customer_claims() -> None:
    with pytest.raises(ValueError, match="not among customer_claims"):
        build(
            request(flags={"human_requested": True}),
            customer_claims=["Quiero un humano"],
            evidence_claims={"human_requested": ["texto inventado"]},
        )


@pytest.mark.parametrize("reason", list(ReasonCode))
def test_missing_required_slots_become_questions(reason: ReasonCode) -> None:
    from app.handoff.builder import SLOT_QUESTIONS
    from app.policy.rules import REQUIRED_SLOTS

    empty = {slot.value: None for slot in REQUIRED_SLOTS[reason]}
    req = request(slots=slots(reason_code=reason, **empty), flags={"human_requested": True})
    questions = build(req).open_questions
    for slot in REQUIRED_SLOTS[reason]:
        assert SLOT_QUESTIONS[slot] in questions, slot
    filled = request(
        slots=slots(
            reason_code=reason,
            card_in_possession=True,
            shared_credentials=False,
            duplicate_ref="TXN-0",
            expected_amount=Decimal("10"),
            expected_delivery_date=date(2026, 6, 1),
            merchant_contacted=True,
        ),
        flags={"human_requested": True},
    )
    assert not set(SLOT_QUESTIONS.values()) & set(build(filled).open_questions)


def test_every_required_slot_has_a_question() -> None:
    from app.handoff.builder import SLOT_QUESTIONS
    from app.policy.rules import REQUIRED_SLOTS

    assert {s for slots in REQUIRED_SLOTS.values() for s in slots} == set(SLOT_QUESTIONS)


def test_disputes_example_asks_for_the_delivery_date() -> None:
    from app.contracts import SlotName
    from app.handoff.builder import SLOT_QUESTIONS
    from app.handoff.examples import example_packets

    questions = example_packets()["disputes"].open_questions
    assert SLOT_QUESTIONS[SlotName.EXPECTED_DELIVERY_DATE] in questions
    assert SLOT_QUESTIONS[SlotName.MERCHANT_CONTACTED] not in questions


def test_fraud_example_evidence_points_to_the_stolen_phone() -> None:
    from app.handoff.examples import example_packets

    packet = example_packets()["fraud"]
    (reason,) = packet.escalation_reasons
    assert reason.evidence[0].claims == ["O celular foi roubado no dia 9 de junho"]
    assert set(reason.evidence[0].claims) <= set(packet.customer_claims)


def test_security_example_names_a_real_pattern() -> None:
    from app.handoff.examples import example_packets
    from app.input_guard.patterns import PATTERNS

    (reason,) = example_packets()["security_review"].escalation_reasons
    pattern = next(e for e in reason.evidence if e.name == "pattern_id")
    assert pattern.value in {p.pattern_id for p in PATTERNS}


def test_business_date_and_transaction_age() -> None:
    packet = full_packet()
    assert packet.business_date == date(2026, 6, 17)
    txn_fact = next(f.fact for f in packet.verified_facts if f.source == "transactions")
    assert txn_fact.endswith("7 days before the business date")
    one_day = build(
        request(
            flags={"human_requested": True},
            transaction_candidates=[txn(when=datetime(2026, 6, 16, 12))],
        )
    )
    assert any(f.fact.endswith("1 day before the business date") for f in one_day.verified_facts)


def test_open_question_dates_match_the_records() -> None:
    from app.handoff.examples import example_packets

    for packet in [full_packet(), *example_packets().values()]:
        # A date in a question must come from a record or from what the customer said.
        known = " ".join([*(f.fact for f in packet.verified_facts), *packet.customer_claims])
        fact_dates = set(re.findall(r"\d{4}-\d{2}-\d{2}", known))
        for question in packet.open_questions:
            assert set(re.findall(r"\d{4}-\d{2}-\d{2}", question)) <= fact_dates, question


def test_model_signals_say_their_source_version_and_calibration() -> None:
    from tests.policy.conftest import with_parameters

    kev = ModelSignals(
        source=ModelSource.KEV,
        model_version="jaredpalmer/kev-0.8b@2026-09-24",
        model_info={"run": "jaredpalmer/kev-0.8b", "release_date": "2026-09-24"},
        reason_code_probs={ReasonCode.INCORRECT_AMOUNT: 0.9},
        reason_code_other=0.1,
        escalation_risk=0.2,
    )
    req = request(signals=kev, flags={"human_requested": True})
    signals = build(req).model_signals
    assert signals.source is ModelSource.KEV
    assert signals.model_info == {"run": "jaredpalmer/kev-0.8b", "release_date": "2026-09-24"}
    assert signals.calibrated is False
    calibrated = PolicyHandoffBuilder(
        with_parameters(ESCALATION_RISK_THRESHOLD=0.7).parameters,
        KEY,
        lambda at: "HO-20261001-000009",
    ).build(
        request=req,
        decision=evaluate(req),
        language=Language.ES,
        customer_claims=[],
        actions_taken=[],
        open_questions=[],
        transcript_ref="CONV-1",
    )
    assert calibrated.model_signals.calibrated is True
    unavailable = build(request(signals=ModelSignals(source=ModelSource.UNAVAILABLE)))
    assert unavailable.model_signals.source is ModelSource.UNAVAILABLE


def test_system_text_comes_from_the_english_catalogue() -> None:
    from app.handoff.builder import RULE_DESCRIPTIONS

    packet = full_packet()
    assert packet.escalation_reasons[0].description == RULE_DESCRIPTIONS["ESC-03"]
    assert set(RULE_DESCRIPTIONS) == {f"ESC-{i:02d}" for i in range(1, 15)}


def test_builder_requires_a_pseudonym_key() -> None:
    with pytest.raises(ValueError, match="PSEUDONYM_KEY"):
        PolicyHandoffBuilder(CONFIG.parameters, "", lambda at: "HO-20261001-000001")


def test_examples_cover_every_route() -> None:
    from app.handoff.examples import ROUTES as EXAMPLE_ROUTES
    from app.handoff.examples import example_packets

    packets = example_packets()
    assert tuple(packets) == EXAMPLE_ROUTES
    assert packets["disputes"].queue is Queue.DISPUTES
    assert packets["fraud"].queue is Queue.FRAUD and packets["fraud"].priority is Priority.HIGH
    assert packets["security_review"].queue is Queue.SECURITY_REVIEW
    assert packets["unauthenticated"].auth.status is AuthStatus.UNAUTHENTICATED
    assert packets["unauthenticated"].verified_facts == []
    assert len({p.handoff_id for p in packets.values()}) == 4
