"""Properties of the engine: purity, determinism, DATA-02, explanations, configuration."""

from __future__ import annotations

import ast
import itertools
import re
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

import app.contracts as contracts
from app.contracts import (
    ActionId,
    CaseStatus,
    Confirmation,
    Outcome,
    PolicyRequest,
    ReasonCode,
)
from app.interfaces import PolicyEngine
from app.policy.rules import GATE_NAMES, ROUTES, RULE_NAMES
from tests.policy.conftest import (
    case,
    engine,
    evaluate,
    request,
    slots,
    txn,
    with_parameters,
)

POLICY_DIR = Path(__file__).resolve().parents[2] / "app" / "policy"
DATA02_FIELDS = {
    "segment",
    "credit_score",
    "estimated_monthly_income",
    "gender",
    "date_of_birth",
    "age",
    "age_band",
    "detected_accent",
    "occupation",
    "marital_status",
    "education_level",
    "country",
}


def test_implements_the_protocol() -> None:
    assert isinstance(engine(), PolicyEngine)
    assert engine().policy_version == "0.4.6"


def test_decision_carries_the_policy_version() -> None:
    assert evaluate(request()).policy_version == "0.4.6"


# --- Purity ----------------------------------------------------------------------------------


def _imported_modules() -> set[str]:
    modules: set[str] = set()
    for path in POLICY_DIR.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                modules |= {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.add(node.module)
    return modules


def test_engine_imports_no_io_clock_or_model_module() -> None:
    allowed_prefixes = (
        "__future__",
        "dataclasses",
        "datetime",
        "decimal",
        "enum",
        "unicodedata",
        "app.config",
        "app.contracts",
        "app.policy",
        "app.storage.data_contract",
    )
    unexpected = {m for m in _imported_modules() if not m.startswith(allowed_prefixes)}
    assert unexpected == set()


def test_engine_never_reads_the_clock() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in POLICY_DIR.glob("*.py"))
    assert not re.search(r"\b(now|today|utcnow|time\.time)\(", source)


# --- Determinism -----------------------------------------------------------------------------


SCENARIOS = [
    request(),
    request(slots=slots(confirmation=Confirmation.CONFIRMED)),
    request(slots=slots(reason_code=ReasonCode.UNRECOGNIZED)),
    request(flags={"human_requested": True, "account_takeover_reported": True}),
    request(transaction_candidates=[txn(amount_usd=None, fraud_score=None)]),
    request(cases=[case(f"CASE-{i}", status=CaseStatus.CLOSED) for i in range(3)]),
]


@pytest.mark.parametrize("req", SCENARIOS)
def test_same_input_same_output(req: PolicyRequest) -> None:
    first = engine().evaluate(req)
    assert all(engine().evaluate(req) == first for _ in range(5))
    assert engine().explain(first) == engine().explain(first)


def test_record_order_does_not_change_the_decision() -> None:
    from datetime import timedelta
    from decimal import Decimal

    from app.contracts import TransactionRef
    from tests.policy.conftest import TXN_DATE, product

    pool = [txn(f"TXN-{i}", when=TXN_DATE + timedelta(hours=i)) for i in range(3)]
    products = [product("PRD-1"), product("PRD-2", last4="2222")]
    cases = [case("CASE-1"), case("CASE-2", status=CaseStatus.RESOLVED)]
    ref_slots = slots(transaction_ref=TransactionRef(amount=Decimal("50")))
    decisions = {
        evaluate(
            request(
                transaction_candidates=list(p), products=list(q), cases=list(c), slots=ref_slots
            )
        ).model_dump_json()
        for p, q, c in itertools.product(
            itertools.permutations(pool), itertools.permutations(products), [cases, cases[::-1]]
        )
    }
    assert len(decisions) == 1


# --- DATA-02 ---------------------------------------------------------------------------------


def _field_names(model: type[BaseModel], seen: set[type[BaseModel]]) -> set[str]:
    if model in seen:
        return set()
    seen.add(model)
    names = set(model.model_fields)
    for field in model.model_fields.values():
        for name in re.findall(r"\w+", repr(field.annotation)):
            candidate = getattr(contracts, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseModel):
                names |= _field_names(candidate, seen)
    return names


def test_no_data02_field_reaches_the_engine() -> None:
    assert _field_names(PolicyRequest, set()) & DATA02_FIELDS == set()


@pytest.mark.parametrize("field", sorted(DATA02_FIELDS))
def test_policy_request_rejects_data02_fields(field: str) -> None:
    values = request().model_dump()
    values[field] = "x"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        PolicyRequest.model_validate(values)


@pytest.mark.parametrize("field", sorted(DATA02_FIELDS))
def test_records_reject_data02_fields(field: str) -> None:
    customer = request().customer
    assert customer is not None
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        contracts.CustomerRecord.model_validate({**customer.model_dump(), field: "x"})


def test_engine_source_mentions_no_data02_field() -> None:
    source = "\n".join(p.read_text(encoding="utf-8") for p in POLICY_DIR.glob("*.py"))
    # "age" alone is the transaction and session age (GATE-02, GATE-08), not the customer's.
    found = {name for name in DATA02_FIELDS - {"age"} if re.search(rf"\b{name}\b", source)}
    assert found == set()


def test_customers_differing_only_in_identity_get_the_same_decision() -> None:
    # Everything the engine sees about two customers with different DATA-02 attributes is the
    # same once their IDs are pseudonymous, so their decisions must be equal up to the IDs.
    first = evaluate(request()).model_dump_json()
    second = request().model_dump_json().replace("CUS-1", "CUS-2")
    other = evaluate(PolicyRequest.model_validate_json(second)).model_dump_json()
    assert first == other


# --- Explanations ----------------------------------------------------------------------------


def test_explain_names_rules_and_records() -> None:
    req = request(
        transaction_candidates=[txn(amount="5000", amount_usd=None, fraud_score=None)],
        slots=slots(
            reason_code=ReasonCode.UNRECOGNIZED, card_in_possession=False, shared_credentials=False
        ),
        flags={"human_requested": True},
    )
    lines = engine().explain(evaluate(req))
    text = "\n".join(lines)
    assert lines[0] == "Outcome ESCALATE under policy 0.4.6."
    assert "GATE-11 (No duplicate case) passed." in lines
    assert "Transaction TXN-1, RC_UNRECOGNIZED." in lines
    assert "Tier T3 (unknown USD amount)." in lines
    assert "ESC-01 (High amount) fired." in lines
    assert "ESC-05 (Human requested) fired." in lines
    assert "Routed to the disputes queue, normal priority." in lines
    assert "ACT-03 (Temporarily block a card) authorized." in lines
    assert "Card PRD-1 may be blocked." in lines
    assert "Note: fraud_score_missing." in lines
    assert "USD" not in text.replace("unknown USD", "")


def test_explain_inform_clarify_and_resolve() -> None:
    eng = engine()
    informed = eng.explain(evaluate(request(cases=[case("CASE-9", transaction_id="TXN-1")])))
    assert "Inform: duplicate_case." in informed
    assert "Existing case CASE-9 (Open)." in informed

    from datetime import timedelta
    from decimal import Decimal

    from app.contracts import TransactionRef
    from tests.policy.conftest import TXN_DATE, product

    pool = [txn("TXN-1"), txn("TXN-2", when=TXN_DATE + timedelta(hours=1))]
    listed = eng.explain(
        evaluate(
            request(
                transaction_candidates=pool,
                slots=slots(transaction_ref=TransactionRef(amount=Decimal("50"))),
            )
        )
    )
    assert "Clarify: transaction_ref." in listed
    assert "Candidates: TXN-2, TXN-1." in listed

    duplicate = eng.explain(
        evaluate(
            request(
                transaction_candidates=pool,
                slots=slots(
                    reason_code=ReasonCode.DUPLICATE,
                    transaction_ref=TransactionRef(transaction_id="TXN-2"),
                ),
            )
        )
    )
    assert "Duplicate pair with TXN-1." in duplicate

    resolved = eng.explain(evaluate(request(slots=slots(confirmation=Confirmation.CONFIRMED))))
    assert "Tier T1 (USD 50)." in resolved
    assert "ACT-02 (Create a dispute case) authorized." in resolved

    blocked = eng.explain(
        evaluate(
            request(
                slots=slots(reason_code=ReasonCode.UNRECOGNIZED),
                products=[product(status="Blocked")],
            )
        )
    )
    assert "The card is already blocked." in blocked


def test_explain_without_transaction() -> None:
    from tests.policy.conftest import unauthenticated

    lines = engine().explain(evaluate(unauthenticated()))
    assert lines == [
        "Outcome CLARIFY under policy 0.4.6.",
        "GATE-01 (Supported language) passed.",
        "GATE-02 (Authenticated session) did not pass.",
        "Clarify: authentication.",
    ]


def test_explain_reason_not_yet_known() -> None:
    lines = engine().explain(evaluate(request(slots=slots(reason_code=None))))
    assert "Transaction TXN-1, no reason code yet." in lines


# --- Configuration and tables ----------------------------------------------------------------


def test_parameters_come_from_the_configuration() -> None:
    stricter = with_parameters(PROVISIONAL_CREDIT_AUTO_MAX_USD=10, AUTO_INTAKE_MAX_USD=40)
    decision = evaluate(request(slots=slots(confirmation=Confirmation.CONFIRMED)), stricter)
    assert decision.outcome is Outcome.ESCALATE
    assert decision.triggered_rules == ["ESC-01"]


def test_rule_tables_cover_the_policy() -> None:
    expected_rules = {f"ESC-{i:02d}" for i in range(1, 15)}
    assert set(ROUTES) == expected_rules == set(RULE_NAMES)
    assert set(GATE_NAMES) == {f"GATE-{i:02d}" for i in range(1, 12)}


def test_resolve_never_authorizes_without_confirmation() -> None:
    for confirmation in (None, Confirmation.HEDGED, Confirmation.DECLINED, Confirmation.WITHDRAWN):
        decision = evaluate(request(slots=slots(confirmation=confirmation)))
        assert ActionId.CREATE_CASE not in decision.authorized_actions
