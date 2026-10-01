"""Example handoff packets, one per route, from synthetic records through the real Policy
Engine and Handoff Builder. Used by ``scripts/show_handoff_example.py`` (packet review, M15,
the demo) and by the tests. No database and no model calls.

The model signals of each example are what that turn would really have: Kev's answer about the
reason the customer gave; the extraction fallback (``derive_fallback``) when Kev did not answer;
and ``unavailable`` when the Input Guard flagged the message, which then reaches neither the LLM
nor Kev.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.config import PolicyConfig, load_policy_config
from app.contracts import (
    ActionId,
    ConversationCounters,
    ConversationFlags,
    CustomerRecord,
    ExtractionResult,
    HandoffPacket,
    InputGuardResult,
    Language,
    ModelSignals,
    ModelSource,
    PolicyRequest,
    ProductRecord,
    ReasonCode,
    SessionContext,
    SlotName,
    Slots,
    ToolResult,
    ToolStatus,
    TransactionRecord,
    TransactionRef,
)
from app.decision.fallback import derive_fallback
from app.handoff.builder import PolicyHandoffBuilder
from app.policy.engine import DeterministicPolicyEngine

EXAMPLE_KEY = "example-pseudonym-key"  # examples only; production uses PSEUDONYM_KEY
NOW = datetime(2026, 10, 1, 15, 4, tzinfo=UTC)
AS_OF = datetime(2026, 6, 18, 6, 0)  # BUSINESS_DATE 2026-06-17 at the 06:00 cutoff
CUSTOMER_ID = "CLI-EXAMPLE00001"
ROUTES = ("disputes", "fraud", "security_review", "unauthenticated")

# request, conversation language, customer claims, actions run, claims behind each slot/flag
Scenario = tuple[PolicyRequest, Language, list[str], list[ToolResult], dict[str, list[str]]]

_SESSION = SessionContext(
    session_id="SES-EXAMPLE",
    customer_id=CUSTOMER_ID,
    auth_method="test_otp",
    issued_at=NOW - timedelta(minutes=6),
    last_activity_at=NOW - timedelta(minutes=1),
)
_CARD = ProductRecord(
    product_id="PRD-EXAMPLECARD1",
    customer_id=CUSTOMER_ID,
    product_type="Tarjeta Crédito",
    product_number_masked="****4821",
    currency="USD",
    product_status="Active",
)
_PURCHASE = TransactionRecord(
    transaction_id="TRX-EXAMPLE00001",
    customer_id=CUSTOMER_ID,
    product_id=_CARD.product_id,
    transaction_type="Purchase",
    transaction_status="Approved",
    transaction_date=datetime(2026, 6, 10, 14, 30),
    amount=Decimal("50.00"),
    currency="USD",
    amount_usd=Decimal("50.00"),
    merchant_name="Cafe Sintetico",
    fraud_score=12.5,
)
_LAPTOP = TransactionRecord(
    transaction_id="TRX-EXAMPLE00002",
    customer_id=CUSTOMER_ID,
    product_id=_CARD.product_id,
    transaction_type="Purchase",
    transaction_status="Approved",
    transaction_date=datetime(2026, 6, 12, 11, 5),
    amount=Decimal("1450.00"),
    currency="USD",
    amount_usd=Decimal("1450.00"),
    merchant_name="Electro Mundo",
    fraud_score=18.0,
)
_KEV_SERVING = {
    "model_version": "jaredpalmer/kev-0.8b@2026-09-24",
    "model_info": {"run": "jaredpalmer/kev-0.8b", "release_date": "2026-09-24"},
}


def _kev(probs: dict[ReasonCode, float], other: float, risk: float) -> ModelSignals:
    """Kev's answer: a complete distribution over the five codes plus OTHER."""
    return ModelSignals(
        source=ModelSource.KEV,
        reason_code_probs=probs,
        reason_code_other=other,
        ambiguity=0.52,
        escalation_risk=risk,
        **_KEV_SERVING,  # type: ignore[arg-type]
    )


_KEV_NOT_RECEIVED = _kev(
    {
        ReasonCode.UNRECOGNIZED: 0.02,
        ReasonCode.DUPLICATE: 0.01,
        ReasonCode.INCORRECT_AMOUNT: 0.04,
        ReasonCode.NOT_RECEIVED: 0.9,
        ReasonCode.FEE: 0.0,
    },
    other=0.03,
    risk=0.55,
)
_KEV_UNRECOGNIZED = _kev(
    {
        ReasonCode.UNRECOGNIZED: 0.94,
        ReasonCode.DUPLICATE: 0.03,
        ReasonCode.INCORRECT_AMOUNT: 0.01,
        ReasonCode.NOT_RECEIVED: 0.01,
        ReasonCode.FEE: 0.0,
    },
    other=0.01,
    risk=0.58,
)


def _request(**values: object) -> PolicyRequest:
    fields: dict[str, object] = {
        "now": NOW,
        "as_of": AS_OF,
        "conversation_id": "CONV-EXAMPLE",
        "detected_language": "es",
        "session": _SESSION,
        "customer": CustomerRecord(customer_id=CUSTOMER_ID, customer_status="Active"),
        "transaction_candidates": [_PURCHASE, _LAPTOP],
        "products": [_CARD],
    }
    fields.update(values)
    return PolicyRequest.model_validate(fields)


def example_packets(config: PolicyConfig | None = None) -> dict[str, HandoffPacket]:
    """One packet per route: disputes, fraud, security_review, and before authentication."""
    config = config or load_policy_config()
    engine = DeterministicPolicyEngine(config)
    numbers = iter(range(1, 100))
    builder = PolicyHandoffBuilder(
        config.parameters,
        EXAMPLE_KEY,
        lambda at: f"HO-{at:%Y%m%d}-{next(numbers):06d}",
        clock=lambda: NOW,
    )
    blocked = ToolResult(
        action=ActionId.BLOCK_CARD,
        status=ToolStatus.SUCCESS,
        verified=True,
        attempts=1,
        record_id=_CARD.product_id,
        detail="Card ending 4821 blocked",
        completed_at=NOW - timedelta(seconds=40),
    )
    scenarios: dict[str, Scenario] = {
        # T3 amount and a request for a person: disputes queue.
        "disputes": (
            _request(
                slots=Slots(
                    transaction_ref=TransactionRef(transaction_id=_LAPTOP.transaction_id),
                    reason_code=ReasonCode.NOT_RECEIVED,
                    # The customer asked for a person before giving the delivery date.
                    merchant_contacted=True,
                ),
                flags=ConversationFlags(human_requested=True),
                signals=_KEV_NOT_RECEIVED,
            ),
            Language.ES,
            ["La laptop nunca llegó", "Ya le escribí a la tienda", "Quiero hablar con una persona"],
            [],
            {
                "merchant_contacted": ["Ya le escribí a la tienda"],
                "human_requested": ["Quiero hablar con una persona"],
            },
        ),
        # Unrecognized purchase, stolen phone, card blocked first: fraud queue, high priority.
        "fraud": (
            _request(
                slots=Slots(
                    transaction_ref=TransactionRef(transaction_id=_PURCHASE.transaction_id),
                    reason_code=ReasonCode.UNRECOGNIZED,
                    card_in_possession=True,
                    shared_credentials=False,
                ),
                flags=ConversationFlags(account_takeover_reported=True),
                signals=_KEV_UNRECOGNIZED,
            ),
            Language.PT,
            ["Não fiz essa compra", "O celular foi roubado no dia 9 de junho"],
            [blocked],
            {"account_takeover_reported": ["O celular foi roubado no dia 9 de junho"]},
        ),
        # Second manipulation attempt: security review.
        "security_review": (
            _request(
                slots=Slots(),
                counters=ConversationCounters(injection_strikes=2),
                input_guard=InputGuardResult(
                    flagged=True,
                    strikes=2,
                    pattern_id="override.ignore_rules.es",  # a real pattern of the catalogue
                    escalate_security=True,
                ),
                # The flagged message reached neither the LLM nor Kev.
                signals=ModelSignals(source=ModelSource.UNAVAILABLE),
            ),
            Language.ES,
            [],
            [],
            {},
        ),
        # A person is requested before authenticating: no account data in the packet.
        "unauthenticated": (
            _request(
                session=None,
                customer=None,
                transaction_candidates=[],
                products=[],
                # Kev did not answer: the fallback derived from this turn's extraction.
                signals=derive_fallback(
                    ExtractionResult(flags=ConversationFlags(human_requested=True))
                ),
                flags=ConversationFlags(human_requested=True),
            ),
            Language.ES,
            ["Quiero hablar con una persona"],
            [],
            {"human_requested": ["Quiero hablar con una persona"]},
        ),
    }
    packets: dict[str, HandoffPacket] = {}
    for route, (request, language, claims, actions, links) in scenarios.items():
        packets[route] = builder.build(
            request=request,
            decision=engine.evaluate(request),
            language=language,
            customer_claims=claims,
            actions_taken=actions,
            open_questions=[],
            transcript_ref=request.conversation_id,
            evidence_claims=links,
            # In these examples every slot the customer gave was set in the first turn.
            slot_turns={name: 0 for name in SlotName},
        )
    return packets
