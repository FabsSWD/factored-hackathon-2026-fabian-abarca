"""ACT-02 (with ACT-04), ACT-03 and ACT-05: permissions, idempotency, read-back, retries."""

from __future__ import annotations

import inspect
import re
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

import app.tools.service as service
from app.contracts import (
    ActionId,
    CaseStatus,
    DraftCase,
    ProvisionalCreditFlag,
    ReasonCode,
    Tier,
    ToolStatus,
)
from app.storage.models import CardBlock, Case, HandoffPacketRow
from app.tools import DatabaseToolLayer
from tests.fixtures.core_banking import ACCOUNT, CARD, CUSTOMER, OTHER_CARD
from tests.tools.conftest import (
    AS_OF,
    PARAMETERS,
    ToolFactory,
    add_product,
    add_transaction,
    count,
    packet,
    security_events,
)


def transient() -> OperationalError:
    return OperationalError("SELECT 1", {}, Exception("connection lost"))


def failing(times: int, then: Any) -> Any:
    """A callable that raises a transient error ``times`` times, then delegates."""
    calls = {"n": 0}

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        if calls["n"] <= times:
            raise transient()
        return then(*args, **kwargs)

    return wrapper


# --- ACT-02 --------------------------------------------------------------------------------


def test_create_case_t1_with_verified_read_back(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.SUCCESS and result.verified
    assert result.action is ActionId.CREATE_CASE
    assert result.idempotency_key == "TRX-T1-PURCHASE:RC_UNRECOGNIZED"
    assert result.record_id is not None
    assert re.fullmatch(r"DSP-20261001-\d{6}", result.record_id)
    assert result.detail == f"Case {result.record_id} created"
    case = db_session.get(Case, result.record_id)
    assert case is not None
    assert case.customer_id == CUSTOMER
    assert case.status == CaseStatus.OPEN.value
    assert case.provisional_credit_flag == ProvisionalCreditFlag.ELIGIBLE.value
    assert case.business_created_at == AS_OF
    assert case.amount_usd == Decimal("50.00")


def test_create_case_t2_records_requires_review(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    add_transaction(db_session, "TRX-T2", datetime(2026, 6, 16, 12))
    result = tools.create_case("TRX-T2", ReasonCode.INCORRECT_AMOUNT, Tier.T2)
    assert result.status is ToolStatus.SUCCESS
    case = db_session.get(Case, result.record_id)
    assert case is not None
    assert case.provisional_credit_flag == ProvisionalCreditFlag.REQUIRES_REVIEW.value


def test_amount_exactly_100_is_t1(tools: DatabaseToolLayer) -> None:
    # TRX-COP-SOURCE: COP with a supplied amount_usd of exactly 100.00.
    assert tools.create_case("TRX-COP-SOURCE", ReasonCode.UNRECOGNIZED, Tier.T1).verified
    assert not tools.create_case("TRX-COP-SOURCE", ReasonCode.DUPLICATE, Tier.T2).verified


def test_repeated_call_returns_the_same_case(tools: DatabaseToolLayer, db_session: Session) -> None:
    first = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    second = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert second.status is ToolStatus.SUCCESS
    assert second.record_id == first.record_id
    assert second.detail == f"Case {first.record_id} already existed"
    assert count(db_session, Case, transaction_id="TRX-T1-PURCHASE") == 1


@pytest.mark.parametrize(
    ("transaction_id", "tier"),
    [
        ("TRX-T3-PURCHASE", Tier.T1),  # USD 1,500 is T3
        ("TRX-T3-PURCHASE", Tier.T3),  # T3 is never automated
        ("TRX-T1-PURCHASE", Tier.T2),  # USD 50 is T1
        ("TRX-ARS-MISSING", Tier.T1),  # unknown USD amount is T3
    ],
)
def test_inconsistent_tier_is_detected_and_nothing_is_written(
    tools: DatabaseToolLayer, db_session: Session, transaction_id: str, tier: Tier
) -> None:
    result = tools.create_case(transaction_id, ReasonCode.UNRECOGNIZED, tier)
    assert result.status is ToolStatus.FAILED and not result.verified
    assert result.error is not None and "does not match the amount" in result.error
    assert count(db_session, Case) == 0


def test_foreign_transaction_is_denied(tools: DatabaseToolLayer, db_session: Session) -> None:
    result = tools.create_case("TRX-OTHER-CUSTOMER", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.ACCESS_DENIED
    assert result.record_id is None
    assert count(db_session, Case) == 0
    assert security_events(db_session)[0]["action"] == "ACT-02"


def test_unauthenticated_create_case_is_denied(make_tools: ToolFactory) -> None:
    result = make_tools(None).create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.ACCESS_DENIED


@pytest.mark.parametrize(
    "change",
    [
        {"tier": Tier.T2},
        {"amount": Decimal("49.00")},
        {"amount_usd": Decimal("49.00")},
        {"currency": "COP"},
        {"reason_code": ReasonCode.DUPLICATE},
        {"transaction_id": "TRX-NO-SCORE"},
        {"provisional_credit_flag": None},
    ],
)
def test_read_back_that_does_not_match_is_a_failure(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch, change: dict[str, Any]
) -> None:
    original = DatabaseToolLayer._read_back_case

    def altered(self: DatabaseToolLayer, customer_id: str, key: str) -> Any:
        stored = original(self, customer_id, key)
        assert stored is not None
        return stored.model_copy(update=change)

    monkeypatch.setattr(DatabaseToolLayer, "_read_back_case", altered)
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.FAILED
    assert result.error == "read_back_mismatch"
    assert result.record_id is None  # the case reference is never shown


def test_missing_read_back_is_a_failure(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(DatabaseToolLayer, "_read_back_case", lambda self, c, k: None)
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.error == "read_back_mismatch"


def test_transient_errors_are_retried_up_to_two_times(
    tools: DatabaseToolLayer, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DatabaseToolLayer, "_new_case_id", failing(2, DatabaseToolLayer._new_case_id)
    )
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.SUCCESS
    assert result.attempts == 3
    assert count(db_session, Case) == 1


def test_three_transient_errors_fail_cleanly(
    tools: DatabaseToolLayer, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DatabaseToolLayer, "_new_case_id", failing(3, DatabaseToolLayer._new_case_id)
    )
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.FAILED
    assert result.attempts == PARAMETERS.TOOL_MAX_RETRIES + 1
    assert result.error == "OperationalError after 3 attempts"
    assert result.idempotency_key == "TRX-T1-PURCHASE:RC_UNRECOGNIZED"
    assert count(db_session, Case) == 0


def test_retry_after_a_committed_write_does_not_duplicate(
    tools: DatabaseToolLayer, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The insert commits, then the read-back loses the connection once: the retry finds the
    # case through the idempotency key instead of creating a second one.
    monkeypatch.setattr(
        DatabaseToolLayer, "_read_back_case", failing(1, DatabaseToolLayer._read_back_case)
    )
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.SUCCESS and result.attempts == 2
    assert result.detail is not None and result.detail.endswith("already existed")
    assert count(db_session, Case) == 1


def test_retry_count_comes_from_the_policy(
    make_tools: ToolFactory, db_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    strict = make_tools(parameters=PARAMETERS.model_copy(update={"TOOL_MAX_RETRIES": 0}))
    monkeypatch.setattr(
        DatabaseToolLayer, "_new_case_id", failing(1, DatabaseToolLayer._new_case_id)
    )
    result = strict.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.status is ToolStatus.FAILED and result.attempts == 1


# --- ACT-03 --------------------------------------------------------------------------------


def test_block_card_and_verify(tools: DatabaseToolLayer, db_session: Session) -> None:
    result = tools.block_card(CARD)
    assert result.status is ToolStatus.SUCCESS and result.verified
    assert result.record_id == CARD
    assert result.detail == "Card ending 4821 blocked"
    assert tools.get_product(CARD).product_status == "Blocked"
    assert count(db_session, CardBlock, product_id=CARD) == 1


def test_block_card_is_idempotent(tools: DatabaseToolLayer, db_session: Session) -> None:
    tools.block_card(CARD)
    again = tools.block_card(CARD)
    assert again.status is ToolStatus.SUCCESS and again.verified
    assert count(db_session, CardBlock) == 1


def test_card_already_blocked_in_core_banking_needs_no_write(
    other_tools: DatabaseToolLayer, db_session: Session
) -> None:
    result = other_tools.block_card(OTHER_CARD)  # Blocked in the source data
    assert result.status is ToolStatus.SUCCESS
    assert count(db_session, CardBlock) == 0


def test_foreign_card_is_rejected(tools: DatabaseToolLayer, db_session: Session) -> None:
    result = tools.block_card(OTHER_CARD)
    assert result.status is ToolStatus.ACCESS_DENIED
    assert count(db_session, CardBlock) == 0
    event = security_events(db_session)[0]
    assert event["action"] == "ACT-03" and event["requested_id"] == OTHER_CARD


def test_only_cards_can_be_blocked(tools: DatabaseToolLayer, db_session: Session) -> None:
    result = tools.block_card(ACCOUNT)
    assert result.status is ToolStatus.FAILED
    assert result.error == "product is not a card"
    assert count(db_session, CardBlock) == 0


@pytest.mark.parametrize("status", ["Closed", "Suspended"])
def test_closed_or_suspended_card_is_not_blocked(
    tools: DatabaseToolLayer, db_session: Session, status: str
) -> None:
    add_product(db_session, "PRD-CARD-X", "Tarjeta Débito", status)
    result = tools.block_card("PRD-CARD-X")
    assert result.status is ToolStatus.FAILED
    assert result.error == f"card is {status}"


def test_block_read_back_mismatch_is_a_failure(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = DatabaseToolLayer._read_back_product

    def still_active(self: DatabaseToolLayer, customer_id: str, product_id: str) -> Any:
        stored = original(self, customer_id, product_id)
        assert stored is not None
        return stored.model_copy(update={"product_status": "Active"})

    monkeypatch.setattr(DatabaseToolLayer, "_read_back_product", still_active)
    result = tools.block_card(CARD)
    assert result.status is ToolStatus.FAILED and result.error == "read_back_mismatch"


def test_block_card_retries_transient_errors(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DatabaseToolLayer, "_read_back_product", failing(2, DatabaseToolLayer._read_back_product)
    )
    result = tools.block_card(CARD)
    assert result.status is ToolStatus.SUCCESS and result.attempts == 3


def test_unauthenticated_block_is_denied(make_tools: ToolFactory) -> None:
    assert make_tools(None).block_card(CARD).status is ToolStatus.ACCESS_DENIED


# --- ACT-05 --------------------------------------------------------------------------------


def test_transfer_writes_and_acknowledges_the_packet(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    sent = packet(draft_case=DraftCase(transaction_ref="TRX-T1-PURCHASE", tier=Tier.T1))
    result = tools.transfer_to_human(sent)
    assert result.status is ToolStatus.SUCCESS and result.verified
    assert result.record_id == sent.handoff_id
    row = db_session.get(HandoffPacketRow, sent.handoff_id)
    assert row is not None
    assert row.status == "acknowledged"
    assert row.customer_id == CUSTOMER
    assert row.transaction_id == "TRX-T1-PURCHASE"
    assert row.packet == sent.model_dump(mode="json")


def test_transfer_is_idempotent(tools: DatabaseToolLayer, db_session: Session) -> None:
    tools.transfer_to_human(packet())
    again = tools.transfer_to_human(packet())
    assert again.status is ToolStatus.SUCCESS
    assert count(db_session, HandoffPacketRow) == 1


def test_transfer_before_authentication(make_tools: ToolFactory, db_session: Session) -> None:
    result = make_tools(None).transfer_to_human(packet())
    assert result.status is ToolStatus.SUCCESS
    row = db_session.get(HandoffPacketRow, "HO-20261001-000001")
    assert row is not None and row.customer_id is None


def test_transfer_with_a_foreign_draft_case_is_denied(
    tools: DatabaseToolLayer, make_tools: ToolFactory, db_session: Session
) -> None:
    foreign = packet(draft_case=DraftCase(transaction_ref="TRX-OTHER-CUSTOMER"))
    assert tools.transfer_to_human(foreign).status is ToolStatus.ACCESS_DENIED
    own = packet(draft_case=DraftCase(transaction_ref="TRX-T1-PURCHASE"))
    assert make_tools(None).transfer_to_human(own).status is ToolStatus.ACCESS_DENIED
    assert count(db_session, HandoffPacketRow) == 0


def test_transfer_read_back_mismatch_is_a_failure(
    tools: DatabaseToolLayer, db_session: Session
) -> None:
    tools.transfer_to_human(packet(request_summary="An earlier packet with the same ID."))
    result = tools.transfer_to_human(packet())
    assert result.status is ToolStatus.FAILED and result.error == "read_back_mismatch"


def test_transfer_retries_transient_errors(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        DatabaseToolLayer, "_acknowledge", failing(3, DatabaseToolLayer._acknowledge)
    )
    result = tools.transfer_to_human(packet())
    assert result.status is ToolStatus.FAILED and result.attempts == 3


# --- ACT-06 --------------------------------------------------------------------------------


FORBIDDEN = (
    "refund",
    "reverse",
    "credit",
    "close",
    "reopen",
    "unblock",
    "contact",
    "email",
    "phone",
    "address",
    "transfer_money",
    "move_money",
    "pay",
    "change",
    "update",
    "delete",
)


def test_no_prohibited_action_exists() -> None:
    public = [
        name
        for name, _ in inspect.getmembers(DatabaseToolLayer, inspect.isfunction)
        if not name.startswith("_")
    ]
    assert sorted(public) == [
        "block_card",
        "create_case",
        "get_case",
        "get_customer",
        "get_product",
        "get_transaction",
        "list_cases",
        "list_products",
        "transaction_candidates",
        "transfer_to_human",
    ]
    for name in public:
        assert not any(word in name for word in FORBIDDEN), name


def test_tool_layer_never_deletes_or_updates_records() -> None:
    source = Path(service.__file__).read_text(encoding="utf-8")
    assert not re.search(r"\b(delete|update)\(", source)
    assert "UPDATE " not in source.upper().replace("UPDATE(", "")


def test_read_back_of_an_unknown_key_is_none(tools: DatabaseToolLayer) -> None:
    assert tools._read_back_case(CUSTOMER, "TRX-NOBODY:RC_FEE") is None


def test_default_clock_is_timezone_aware() -> None:
    assert service.utc_now().tzinfo is not None


# --- Case lookup by number and action times ---------------------------------------------------


def test_case_number_lookup_is_filtered_by_the_session_customer(
    tools: DatabaseToolLayer, other_tools: DatabaseToolLayer, db_session: Session
) -> None:
    from app.contracts import AccessDeniedError

    created = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert created.record_id is not None
    assert tools.get_case(created.record_id).transaction_id == "TRX-T1-PURCHASE"
    # Case numbers are sequential: another customer guessing one gets access_denied.
    with pytest.raises(AccessDeniedError):
        other_tools.get_case(created.record_id)
    with pytest.raises(AccessDeniedError):
        other_tools.get_case("DSP-20261001-999999")
    events = security_events(db_session)
    assert [e["record_type"] for e in events] == ["case", "case"]


def test_every_result_has_its_completion_time(
    tools: DatabaseToolLayer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.tools.conftest import NOW

    results = [
        tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1),
        tools.create_case("TRX-T3-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1),  # failed
        tools.block_card(OTHER_CARD),  # access denied
        tools.block_card(CARD),
        tools.transfer_to_human(packet()),
    ]
    assert {r.completed_at for r in results} == {NOW}
