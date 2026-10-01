"""One read function applies the card_blocks overlay (app/storage/repositories.py), and every
path that reads product_status goes through it: GATE-09, the ACT-03 offer and "already blocked",
and the handoff facts. Card unblocking by an agent is out of scope (ACT-06)."""

from __future__ import annotations

from sqlalchemy.orm import Session, sessionmaker

from app.config import load_policy_config
from app.contracts import (
    ActionId,
    ConversationFlags,
    Language,
    ModelSignals,
    ModelSource,
    Outcome,
    PolicyDecision,
    PolicyRequest,
    ReasonCode,
    SessionContext,
    Slots,
    TransactionRef,
)
from app.handoff import PolicyHandoffBuilder
from app.policy import DeterministicPolicyEngine
from app.storage.models import CardBlock
from app.tools import DatabaseToolLayer
from tests.fixtures.core_banking import CARD, CUSTOMER
from tests.tools.conftest import AS_OF, NOW, PARAMETERS

CONFIG = load_policy_config()


def policy_request(tools: DatabaseToolLayer, **values: object) -> PolicyRequest:
    """A request built only from Tool Layer reads, as the Orchestrator will build it."""
    fields: dict[str, object] = {
        "now": NOW,
        "as_of": AS_OF,
        "conversation_id": "CONV-1",
        "detected_language": "es",
        "session": SessionContext(
            session_id="SES-1",
            customer_id=CUSTOMER,
            auth_method="test_otp",
            issued_at=NOW,
            last_activity_at=NOW,
        ),
        "slots": Slots(
            transaction_ref=TransactionRef(transaction_id="TRX-T1-PURCHASE"),
            reason_code=ReasonCode.UNRECOGNIZED,
        ),
        "signals": ModelSignals(source=ModelSource.LLM_FALLBACK),
        "customer": tools.get_customer(),
        "transaction_candidates": tools.transaction_candidates("TRX-T1-PURCHASE"),
        "products": tools.list_products(),
        "cases": tools.list_cases(),
    }
    fields.update(values)
    return PolicyRequest.model_validate(fields)


def evaluate(request: PolicyRequest) -> PolicyDecision:
    return DeterministicPolicyEngine(CONFIG).evaluate(request)


def test_reads_report_the_block(tools: DatabaseToolLayer, db_session: Session) -> None:
    assert tools.get_product(CARD).product_status == "Active"
    db_session.add(CardBlock(product_id=CARD, customer_id=CUSTOMER))
    db_session.flush()
    assert tools.get_product(CARD).product_status == "Blocked"
    assert {p.product_status for p in tools.list_products() if p.product_id == CARD} == {"Blocked"}


def test_gate09_passes_for_a_card_blocked_by_act03(tools: DatabaseToolLayer) -> None:
    assert tools.block_card(CARD).verified
    decision = evaluate(policy_request(tools))
    assert next(g.passed for g in decision.gates_evaluated if g.gate_id == "GATE-09")


def test_act03_offer_before_and_already_blocked_after(tools: DatabaseToolLayer) -> None:
    before = evaluate(policy_request(tools))
    assert ActionId.BLOCK_CARD in before.authorized_actions
    assert before.card_product_id == CARD
    assert tools.block_card(CARD).verified
    after = evaluate(policy_request(tools))
    assert ActionId.BLOCK_CARD not in after.authorized_actions
    assert after.card_already_blocked is True


def test_handoff_facts_after_the_block(
    tools: DatabaseToolLayer, session_factory: sessionmaker[Session]
) -> None:
    blocked = tools.block_card(CARD)
    flags = ConversationFlags(human_requested=True)
    # Read again after the action: the request carries the overlay.
    request = policy_request(tools, flags=flags)
    decision = evaluate(request)
    assert decision.outcome is Outcome.ESCALATE
    packet = PolicyHandoffBuilder(PARAMETERS, "k", lambda at: "HO-20261001-000001").build(
        request=request,
        decision=decision,
        language=Language.ES,
        customer_claims=[],
        actions_taken=[blocked],
        open_questions=[],
        transcript_ref="CONV-1",
    )
    card_facts = [f.fact for f in packet.verified_facts if f.record_id == CARD]
    assert card_facts and all("is Active" not in fact for fact in card_facts)
    assert card_facts[0].startswith("Card ending 4821 is Blocked")
