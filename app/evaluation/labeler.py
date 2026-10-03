"""The reference labeler (policy §16.2): the Deterministic Policy Engine on the specification.

The label of a case does not depend on any text or model. For each dispute, in order, the
labeler builds the ``PolicyRequest`` the conversation would end with when the customer gave
the specified final values (the identified transaction, the reason and its slots, the
confirmation) and the special conditions held (a session or not, the flags, the counters they
imply, the Input Guard result, unavailable models, a failed action). It carries over what one
dispute leaves for the next: the case it created (ESC-02, GATE-11), a card it blocked, an
unrecognized transaction counted for ESC-03.

The label of a dispute is the engine's outcome, triggered rules, queue, priority, inform reason
and tier, and the actions the conversation is expected to execute: ACT-03 when the block is
offered and confirmed, ACT-02 and ACT-04 on RESOLVE, ACT-05 on ESCALATE. The conversation ends
at the first escalation; later disputes are not reached.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict

from app.config import PolicyConfig, load_policy_config
from app.contracts import (
    ActionId,
    CaseRecord,
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    ConversationCounters,
    ConversationFlags,
    CustomerRecord,
    ExtractionResult,
    InputGuardResult,
    ModelSignals,
    ModelSource,
    Outcome,
    PolicyDecision,
    PolicyRequest,
    ProductRecord,
    ProvisionalCreditFlag,
    ReasonCode,
    SessionContext,
    Slots,
    Tier,
    ToolResult,
    ToolStatus,
    TransactionRecord,
    TransactionRef,
)
from app.decision.fallback import derive_fallback
from app.evaluation.scenarios import Conditions, DisputeSpec, Path3, Scenario, transaction_moment
from app.policy import DeterministicPolicyEngine

LABEL_BUSINESS_DATE = date(2026, 6, 17)
"""Labels are computed on this business date; specifications are relative to it, so a seeded
database with another ``BUSINESS_DATE`` gets the same labels."""
LABEL_NOW = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


class DisputeLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    transaction: str | None  # the specification's key
    outcome: Outcome
    triggered_rules: list[str]
    failed_gates: list[str]  # the gates that did not pass (coverage of §5)
    queue: str | None
    priority: str | None
    inform_reason: str | None
    clarify_target: str | None
    tier: str | None
    reason_code: str | None
    actions: list[str]  # executed: ACT-02, ACT-03, ACT-04, ACT-05


class CaseLabel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    case_id: str
    language: str
    path: Path3
    outcome: Outcome  # of the last dispute reached
    triggered_rules: list[str]  # of every dispute reached
    disputes: list[DisputeLabel]


def tier_of(amount_usd: Decimal | None, config: PolicyConfig) -> Tier:
    p = config.parameters
    if amount_usd is None or amount_usd > p.AUTO_INTAKE_MAX_USD:
        return Tier.T3
    return Tier.T2 if amount_usd > p.PROVISIONAL_CREDIT_AUTO_MAX_USD else Tier.T1


class _Context:
    """The records of a case as the Tool Layer would read them."""

    def __init__(self, scenario: Scenario, config: PolicyConfig, business_date: date) -> None:
        self.scenario, self.config, self.business_date = scenario, config, business_date
        self.as_of = datetime.combine(business_date + timedelta(days=1), time(6, 0))
        self.blocked: set[str] = set()
        self.cases: list[CaseRecord] = [
            self._case(
                scenario.case_id(i),
                prior.transaction,
                prior.reason_code,
                prior.status,
                prior.days_ago,
            )
            for i, prior in enumerate(scenario.prior_cases, start=1)
        ]
        self.unrecognized = 0

    def record(self, key: str) -> TransactionRecord:
        spec = self.scenario.transaction(key)
        owner = self.scenario.foreign_customer_id if spec.foreign else self.scenario.customer_id
        product = (
            f"{self.scenario.product_id(spec.product)}X"
            if spec.foreign
            else self.scenario.product_id(spec.product)
        )
        return TransactionRecord(
            transaction_id=self.scenario.transaction_id(key),
            customer_id=owner,
            product_id=product,
            transaction_type=spec.type,
            transaction_status=spec.status,
            transaction_date=transaction_moment(spec, self.business_date),
            amount=spec.amount,
            currency=spec.currency,
            amount_usd=spec.usd,
            merchant_name=spec.merchant,
            merchant_category=spec.category,
            fraud_score=spec.fraud_score,
        )

    def own_transactions(self) -> list[TransactionRecord]:
        return [self.record(t.key) for t in self.scenario.transactions if not t.foreign]

    def products(self) -> list[ProductRecord]:
        return [
            ProductRecord(
                product_id=self.scenario.product_id(p.key),
                customer_id=self.scenario.customer_id,
                product_type=p.type,
                product_number_masked=f"****{p.last4}",
                currency=p.currency,
                product_status="Blocked" if p.key in self.blocked else p.status,
            )
            for p in self.scenario.products
        ]

    def _case(
        self, case_id: str, key: str, reason: ReasonCode, status: CaseStatus, days_ago: int
    ) -> CaseRecord:
        txn = self.record(key)
        tier = tier_of(txn.amount_usd, self.config)
        return CaseRecord(
            case_id=case_id,
            customer_id=self.scenario.customer_id,
            transaction_id=txn.transaction_id,
            reason_code=reason,
            status=status,
            tier=tier,
            amount=txn.amount,
            currency=txn.currency,
            amount_usd=txn.amount_usd or Decimal(0),
            provisional_credit_flag=(
                ProvisionalCreditFlag.ELIGIBLE
                if tier is Tier.T1
                else ProvisionalCreditFlag.REQUIRES_REVIEW
            ),
            created_at=LABEL_NOW,
            business_created_at=self.as_of - timedelta(days=days_ago),
        )

    def created(self, dispute: DisputeSpec, decision: PolicyDecision) -> None:
        assert dispute.transaction is not None and dispute.reason_code is not None
        disputed = decision.transaction_id or self.scenario.transaction_id(dispute.transaction)
        key = disputed.rsplit("-", 1)[1]
        number = len(self.cases) + 1
        self.cases.append(
            self._case(f"LABEL-{number}", key, dispute.reason_code, CaseStatus.OPEN, 0)
        )


def _slots(scenario: Scenario, ctx: _Context, dispute: DisputeSpec) -> Slots:
    reference = None
    if dispute.transaction is not None:
        reference = TransactionRef(transaction_id=scenario.transaction_id(dispute.transaction))
    delivery = (
        ctx.business_date - timedelta(days=dispute.delivery_days_ago)
        if dispute.delivery_days_ago is not None
        else None
    )
    confirmation = (
        Confirmation.WITHDRAWN if dispute.confirmation == "withdrawn" else Confirmation.CONFIRMED
    )
    return Slots(
        transaction_ref=reference,
        reason_code=dispute.reason_code,
        card_in_possession=dispute.card_in_possession,
        shared_credentials=dispute.shared_credentials,
        duplicate_ref=scenario.transaction_id(dispute.duplicate) if dispute.duplicate else None,
        expected_amount=dispute.expected_amount,
        expected_delivery_date=delivery,
        merchant_contacted=dispute.merchant_contacted,
        fee_ref=dispute.fee_ref,
        confirmation=confirmation,
    )


def build_request(
    *,
    case_id: str,
    language: str,
    conditions: Conditions,
    slots: Slots,
    config: PolicyConfig,
    as_of: datetime,
    unrecognized_before: int,
    customer: CustomerRecord | None,
    candidates: list[TransactionRecord],
    products: list[ProductRecord],
    cases: list[CaseRecord],
    foreign: bool,
) -> PolicyRequest:
    """The ``PolicyRequest`` a conversation ends with: the final slots, the flags and counters
    the conditions imply, and the records the Tool Layer reads (seeded or real)."""
    p = config.parameters
    flags = ConversationFlags(
        human_requested=conditions.human_requested,
        account_takeover_reported=conditions.account_takeover_reported,
        legal_or_vulnerability=conditions.legal_or_vulnerability,
        authentication_declined=conditions.authentication_declined,
    )
    signals = (
        ModelSignals(source=ModelSource.UNAVAILABLE)
        if conditions.models_unavailable
        else derive_fallback(ExtractionResult(slots=slots, flags=flags))
    )
    by_slot: dict[ClarifyTarget, int] = {}
    if conditions.unresolved is not None:
        by_slot[conditions.unresolved] = p.MAX_CLARIFICATION_TURNS
    current = 1 if slots.reason_code is ReasonCode.UNRECOGNIZED else 0
    counters = ConversationCounters(
        clarifications_by_slot=by_slot,
        total_clarifications=sum(by_slot.values()),
        language_clarifications=1 if conditions.detected_language else 0,
        authentication_attempts=p.AUTH_MAX_ATTEMPTS
        if conditions.authentication_attempts_exhausted
        else 0,
        injection_strikes=conditions.injection_strikes,
        unrecognized_transactions=unrecognized_before + current,
    )
    guard = None
    if conditions.injection_strikes >= p.INJECTION_STRIKES_MAX:
        guard = InputGuardResult(
            flagged=True,
            strikes=conditions.injection_strikes,
            pattern_id="override.ignore_rules.es",
            escalate_security=True,
        )
    session = None
    if conditions.authenticated and customer is not None:
        session = SessionContext(
            session_id=f"SES-{case_id}",
            customer_id=customer.customer_id,
            auth_method="test_otp",
            issued_at=LABEL_NOW - timedelta(minutes=5),
            last_activity_at=LABEL_NOW,
        )
    return PolicyRequest(
        now=LABEL_NOW,
        as_of=as_of,
        conversation_id=f"CONV-{case_id}",
        detected_language=conditions.detected_language or language,
        session=session,
        slots=slots.model_copy(update={"transaction_ref": None}) if foreign else slots,
        flags=flags,
        signals=signals,
        input_guard=guard,
        counters=counters,
        customer=customer if session else None,
        ownership_violation=foreign,
        transaction_candidates=candidates if session else [],
        products=products if session else [],
        cases=cases if session else [],
    )


def _request(scenario: Scenario, ctx: _Context, dispute: DisputeSpec) -> PolicyRequest:
    foreign = dispute.transaction is not None and scenario.transaction(dispute.transaction).foreign
    return build_request(
        case_id=scenario.id,
        language=scenario.language,
        conditions=scenario.conditions,
        slots=_slots(scenario, ctx, dispute),
        config=ctx.config,
        as_of=ctx.as_of,
        unrecognized_before=ctx.unrecognized,
        customer=CustomerRecord(
            customer_id=scenario.customer_id, customer_status=scenario.customer.status
        ),
        candidates=ctx.own_transactions(),
        products=ctx.products(),
        cases=list(ctx.cases),
        foreign=foreign,
    )


def actions_for(decision: PolicyDecision, block: str | None) -> list[str]:
    """The actions the conversation executes: ACT-03 when the offered block is confirmed,
    ACT-02 and ACT-04 on RESOLVE, ACT-05 on ESCALATE."""
    actions: list[str] = []
    if decision.card_product_id is not None and block == "confirmed":
        actions.append(ActionId.BLOCK_CARD.value)
    if decision.outcome is Outcome.RESOLVE:
        actions += [ActionId.CREATE_CASE.value, ActionId.RECORD_CREDIT_FLAG.value]
    if decision.outcome is Outcome.ESCALATE:
        actions.append(ActionId.TRANSFER_TO_HUMAN.value)
    return actions


def dispute_label(
    decision: PolicyDecision, transaction: str | None, block: str | None
) -> DisputeLabel:
    return DisputeLabel(
        transaction=transaction,
        outcome=decision.outcome,
        triggered_rules=list(decision.triggered_rules),
        failed_gates=[g.gate_id for g in decision.gates_evaluated if not g.passed],
        queue=decision.queue.value if decision.queue else None,
        priority=decision.priority.value if decision.priority else None,
        inform_reason=decision.inform_reason.value if decision.inform_reason else None,
        clarify_target=decision.clarify_target.value if decision.clarify_target else None,
        tier=decision.tier.value if decision.tier else None,
        reason_code=decision.reason_code.value if decision.reason_code else None,
        actions=actions_for(decision, block),
    )


def _failed_case(dispute: DisputeSpec, scenario: Scenario) -> ToolResult:
    assert dispute.transaction is not None and dispute.reason_code is not None
    return ToolResult(
        action=ActionId.CREATE_CASE,
        status=ToolStatus.FAILED,
        verified=False,
        attempts=3,
        idempotency_key=f"{scenario.transaction_id(dispute.transaction)}:{dispute.reason_code.value}",
        error="timeout",
        completed_at=LABEL_NOW,
    )


def label_case(
    scenario: Scenario,
    config: PolicyConfig | None = None,
    business_date: date = LABEL_BUSINESS_DATE,
) -> CaseLabel:
    config = config or load_policy_config()
    engine = DeterministicPolicyEngine(config)
    ctx = _Context(scenario, config, business_date)
    labels: list[DisputeLabel] = []
    for dispute in scenario.disputes:
        request = _request(scenario, ctx, dispute)
        decision = engine.evaluate(request)
        if decision.outcome is Outcome.RESOLVE and scenario.conditions.tool_failure:
            failed = [_failed_case(dispute, scenario)]
            decision = engine.evaluate(request.model_copy(update={"tool_results": failed}))
        labels.append(dispute_label(decision, dispute.transaction, dispute.block))
        if decision.card_product_id is not None and dispute.block == "confirmed":
            key = next(
                p.key
                for p in scenario.products
                if scenario.product_id(p.key) == decision.card_product_id
            )
            ctx.blocked.add(key)
        if decision.outcome is Outcome.ESCALATE:
            break  # automation ends: later disputes are not reached
        if (
            decision.outcome in (Outcome.RESOLVE, Outcome.INFORM)
            and dispute.reason_code is ReasonCode.UNRECOGNIZED
        ):
            ctx.unrecognized += 1
        if decision.outcome is Outcome.RESOLVE:
            ctx.created(dispute, decision)
    rules = sorted({rule for label in labels for rule in label.triggered_rules})
    return CaseLabel(
        case_id=scenario.id,
        language=scenario.language,
        path=scenario.path,
        outcome=labels[-1].outcome,
        triggered_rules=rules,
        disputes=labels,
    )


def label_all(scenarios: list[Scenario], config: PolicyConfig | None = None) -> list[CaseLabel]:
    config = config or load_policy_config()
    return [label_case(scenario, config) for scenario in scenarios]
