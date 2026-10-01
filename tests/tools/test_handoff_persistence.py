"""M10 with the database: handoff IDs from the sequence, and the packet persisted by ACT-05."""

from __future__ import annotations

import re
from datetime import UTC, datetime

from sqlalchemy.orm import Session, sessionmaker

from app.contracts import Language, ModelSignals, ModelSource, PolicyRequest, ToolStatus
from app.handoff import PolicyHandoffBuilder, SequenceHandoffIds
from app.policy import DeterministicPolicyEngine
from app.storage.models import HandoffPacketRow
from app.tools import DatabaseToolLayer
from tests.fixtures.core_banking import CUSTOMER
from tests.tools.conftest import AS_OF, NOW, PARAMETERS

KEY = "test-pseudonym-key"


def test_ids_come_from_the_sequence(session_factory: sessionmaker[Session]) -> None:
    ids = SequenceHandoffIds(session_factory)
    first, second = ids(datetime(2026, 10, 1, tzinfo=UTC)), ids(datetime(2026, 10, 1, tzinfo=UTC))
    assert re.fullmatch(r"HO-20261001-\d{6}", first)
    assert int(second[-6:]) == int(first[-6:]) + 1


def test_built_packet_is_persisted_and_acknowledged(
    tools: DatabaseToolLayer, session_factory: sessionmaker[Session], db_session: Session
) -> None:
    from app.config import load_policy_config
    from app.contracts import ConversationFlags, SessionContext, Slots, TransactionRef

    config = load_policy_config()
    session = SessionContext(
        session_id="SES-1",
        customer_id=CUSTOMER,
        auth_method="test_otp",
        issued_at=NOW,
        last_activity_at=NOW,
    )
    request = PolicyRequest(
        now=NOW,
        as_of=AS_OF,
        conversation_id="CONV-1",
        detected_language="es",
        session=session,
        slots=Slots(transaction_ref=TransactionRef(transaction_id="TRX-T1-PURCHASE")),
        flags=ConversationFlags(human_requested=True),
        signals=ModelSignals(source=ModelSource.LLM_FALLBACK),
        customer=tools.get_customer(),
        transaction_candidates=tools.transaction_candidates("TRX-T1-PURCHASE"),
        products=tools.list_products(),
        cases=tools.list_cases(),
    )
    decision = DeterministicPolicyEngine(config).evaluate(request)
    builder = PolicyHandoffBuilder(
        PARAMETERS, KEY, SequenceHandoffIds(session_factory), clock=lambda: NOW
    )
    packet = builder.build(
        request=request,
        decision=decision,
        language=Language.ES,
        customer_claims=["Wants to talk to a person"],
        actions_taken=[],
        open_questions=[],
        transcript_ref="CONV-1",
    )
    result = tools.transfer_to_human(packet)
    assert result.status is ToolStatus.SUCCESS and result.verified
    row = db_session.get(HandoffPacketRow, packet.handoff_id)
    assert row is not None
    assert row.status == "acknowledged"
    assert row.customer_id == CUSTOMER  # internal link; the packet itself is pseudonymous
    assert CUSTOMER not in str(row.packet)
    assert row.packet == packet.model_dump(mode="json")


def test_ids_past_six_digits_stay_valid_and_unique(
    tools: DatabaseToolLayer, session_factory: sessionmaker[Session], db_session: Session
) -> None:
    from sqlalchemy import text

    from tests.tools.conftest import packet

    db_session.execute(text("SELECT setval('handoff_number_seq', 999999)"))
    ids = SequenceHandoffIds(session_factory)
    big = ids(datetime(2026, 10, 1, tzinfo=UTC))
    assert big == "HO-20261001-1000000"
    assert big != "HO-20261001-000000"
    result = tools.transfer_to_human(packet(handoff_id=big))
    assert result.status is ToolStatus.SUCCESS
    assert db_session.get(HandoffPacketRow, big) is not None


def test_case_numbers_past_six_digits(tools: DatabaseToolLayer, db_session: Session) -> None:
    from sqlalchemy import text

    from app.contracts import ReasonCode, Tier

    db_session.execute(text("SELECT setval('case_number_seq', 999999)"))
    result = tools.create_case("TRX-T1-PURCHASE", ReasonCode.UNRECOGNIZED, Tier.T1)
    assert result.record_id == "DSP-20261001-1000000"
