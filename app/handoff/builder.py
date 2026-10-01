"""M10 Handoff Builder: the policy §13 packet for ESCALATE outcomes.

The packet lets a human agent continue without rereading the conversation:

- ``request_summary`` and ``escalation_reasons`` say what the customer wants and why the case
  escalated, built from templates and verified data, with the evidence of every triggered rule
  (taken from the Policy Engine's decision, never generated).
- ``verified_facts`` are records read through the Tool Layer, each with its source table and
  record ID (DATA-04), as they stand after the actions of the turn: a card blocked by ACT-03 is
  reported as blocked. Transaction ages are counted against ``business_date`` (policy §15).
- ``customer_claims`` are only what the customer said, in the conversation language, kept
  apart from facts (DATA-03). The evidence of a slot or flag points to the claims behind it
  (``evidence_claims``); the system never writes a claim for the customer.
- ``collected_slots`` are the slots the customer already gave, with the turn that set them,
  marked as unverified claims; ``confirmation`` is not one of them. A collected slot is never
  asked again in ``open_questions``.
- Customer-provided text (claims and slot values) passes through the same masking as the audit
  trail (``app.audit.masking.mask_message``, built on the LLM Adapter's DATA-01 minimization).
- ``actions_taken`` lists every attempted action with its verification and time, failed ones
  included; ``open_questions`` says what the agent still needs to establish.

System-generated text (summary, reasons, facts, questions) is in English, the language of the
agent console. The packet never carries the raw transcript (only ``transcript_ref``) or the
customer ID (only a pseudonymous ``customer_ref``). Queue and priority come from the decision,
which already applied the §7 routes. ACT-05 (``ToolLayer.transfer_to_human``) persists it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.audit.masking import mask_message
from app.config import PolicyParameters
from app.contracts import (
    ActionId,
    ActionTaken,
    AuthStatus,
    CaseStatus,
    CollectedSlot,
    DraftCase,
    EscalationReason,
    Evidence,
    EvidenceKind,
    HandoffAuth,
    HandoffModelSignals,
    HandoffPacket,
    Language,
    ModelSource,
    Outcome,
    PolicyDecision,
    PolicyRequest,
    ProductRecord,
    ReasonCode,
    SlotName,
    Slots,
    ToolResult,
    ToolStatus,
    TransactionRecord,
    TransactionRef,
    VerifiedFact,
)
from app.policy.clock import business_date, transaction_age_days
from app.policy.rules import RULE_NAMES, missing_required_slots
from app.pseudonym import UNAUTHENTICATED_REF, customer_ref
from app.storage.data_contract import CARD_PRODUCT_TYPES
from app.tools.provenance import Record, verified_fact

Clock = Callable[[], datetime]
HandoffIds = Callable[[datetime], str]

REASON_NAMES: dict[ReasonCode, str] = {
    ReasonCode.UNRECOGNIZED: "Unrecognized charge",
    ReasonCode.DUPLICATE: "Duplicate charge",
    ReasonCode.INCORRECT_AMOUNT: "Incorrect amount",
    ReasonCode.NOT_RECEIVED: "Goods or services not received",
    ReasonCode.FEE: "Disputed bank fee",
}

RULE_DESCRIPTIONS: dict[str, str] = {
    "ESC-01": "High amount: the USD equivalent is above the automated intake limit, so a "
    "person reviews it before a case exists.",
    "ESC-02": "Dispute velocity: the customer's recent disputes exceed the automated limits.",
    "ESC-03": "Possible account takeover: the customer reported a takeover indicator or "
    "several unrecognized charges.",
    "ESC-04": "High fraud score on the disputed transaction.",
    "ESC-05": "The customer asked for a human agent.",
    "ESC-06": "Legal, regulatory, media, or vulnerability signal from the customer.",
    "ESC-07": "Late filing: the transaction is past the automated filing window but within "
    "the late window.",
    "ESC-08": "The customer or product status does not allow automated intake.",
    "ESC-09": "Information stayed missing or ambiguous after the allowed clarifications.",
    "ESC-10": "An action failed or its result could not be verified.",
    "ESC-11": "The decision model's signals were uncertain or unavailable.",
    "ESC-12": "The conversation language is not supported or stayed unclear.",
    "ESC-13": "Repeated attempts to manipulate the assistant.",
    "ESC-14": "A plausible dispute that is not automated for this transaction type.",
}

# The question for each slot still missing: the two every dispute needs, and the
# reason-specific ones (rules.REQUIRED_SLOTS).
SLOT_QUESTIONS: dict[SlotName, str] = {
    SlotName.TRANSACTION_REF: "Which transaction does the customer want to dispute?",
    SlotName.REASON_CODE: "What is the reason for the dispute?",
    SlotName.CARD_IN_POSSESSION: "Does the customer still have the card?",
    SlotName.SHARED_CREDENTIALS: (
        "Did the customer share credentials or one-time codes with anyone?"
    ),
    SlotName.DUPLICATE_REF: "Which earlier charge does the customer say this one duplicates?",
    SlotName.EXPECTED_AMOUNT: "What amount did the customer agree to pay?",
    SlotName.EXPECTED_DELIVERY_DATE: "When were the goods or services due to be delivered?",
    SlotName.MERCHANT_CONTACTED: "Has the customer contacted the merchant?",
}

# What the agent must establish for each engine note.
NOTE_QUESTIONS: dict[str, str] = {
    "fraud_score_missing": "The transaction has no fraud score: assess the fraud risk manually.",
    "amount_usd_missing": (
        "The USD equivalent could not be established: confirm the amount and the tier."
    ),
    "no_card_to_block": (
        "The disputed product is not a card, so no card block was offered: check whether the "
        "account needs protection."
    ),
    "card_block_not_offered_several_cards": (
        "The customer has several active cards and no transaction was identified: ask which "
        "card to block."
    ),
    "no_active_card": "The customer has no active card to block: check which product is at risk.",
    "customer_record_missing": "The customer record could not be read: verify the customer.",
    "product_record_missing": "The product record could not be read: verify the product.",
    "transaction_ref_missing": "Which transaction does the customer want to dispute?",
    "no_matching_transaction": (
        "No transaction matched the details the customer gave: identify the transaction."
    ),
    "too_many_matches": "Several transactions matched: identify the one the customer means.",
    "transaction_id_inconsistent": (
        "The transaction ID did not match the other details given: confirm the transaction."
    ),
    "transaction_id_not_in_records": "The transaction ID given was not found: confirm it.",
    "duplicate_not_found": (
        "No duplicate charge was found for an RC_DUPLICATE claim: confirm the reason."
    ),
    "duplicate_declined": (
        "The customer did not confirm the duplicate charge found: confirm the reason."
    ),
    "duplicate_ref_not_a_twin": "The duplicate the customer pointed to does not match: verify it.",
    "expected_delivery_date_before_transaction": (
        "The expected delivery date given is before the transaction: confirm it."
    ),
}


class PolicyHandoffBuilder:
    """Implements ``app.interfaces.HandoffBuilder``."""

    def __init__(
        self,
        parameters: PolicyParameters,
        pseudonym_key: str,
        handoff_ids: HandoffIds,
        clock: Clock | None = None,
    ) -> None:
        if not pseudonym_key:
            raise ValueError("PSEUDONYM_KEY is required")
        self._p = parameters
        self._key = pseudonym_key
        self._ids = handoff_ids
        self._clock = clock or (lambda: datetime.now(UTC))

    def build(
        self,
        *,
        request: PolicyRequest,
        decision: PolicyDecision,
        language: Language,
        customer_claims: Sequence[str],
        actions_taken: Sequence[ToolResult],
        open_questions: Sequence[str],
        transcript_ref: str,
        evidence_claims: Mapping[str, Sequence[str]] | None = None,
        slot_turns: Mapping[SlotName, int] | None = None,
    ) -> HandoffPacket:
        """``evidence_claims`` maps a slot or flag name (``shared_credentials``,
        ``account_takeover_reported``) to the customer claims of the turn that set it. Every
        linked claim must be one of ``customer_claims``. ``slot_turns`` gives the turn that set
        each collected slot."""
        if decision.outcome is not Outcome.ESCALATE:
            raise ValueError("a handoff packet is built only for ESCALATE outcomes")
        unknown = {c for claims in (evidence_claims or {}).values() for c in claims} - set(
            customer_claims
        )
        if unknown:
            raise ValueError(f"evidence claims not among customer_claims: {sorted(unknown)}")
        links = {
            name: [mask_message(claim) for claim in claims]
            for name, claims in (evidence_claims or {}).items()
        }
        assert decision.queue is not None and decision.priority is not None
        created_at = self._clock()
        auth = self._auth(request)
        authenticated = auth.status is AuthStatus.AUTHENTICATED
        session = request.session
        reference = customer_ref(session.customer_id, self._key) if session else UNAUTHENTICATED_REF

        # GATE-02: without an authenticated session no account data enters the packet.
        txn = (
            _find(request.transaction_candidates, decision.transaction_id)
            if authenticated
            else None
        )
        blocked = _blocked_cards(actions_taken)
        facts = self._facts(request, decision, reference, blocked) if authenticated else []
        draft = (
            DraftCase(
                transaction_ref=txn.transaction_id,
                amount_usd=decision.amount_usd,
                tier=decision.tier,
                provisional_credit_flag=decision.provisional_credit_flag,
            )
            if txn is not None
            else None
        )
        actions = [
            ActionTaken(
                action=result.action,
                result=result.status,
                verified=result.verified,
                detail=result.detail or result.error,
                at=result.completed_at,
            )
            for result in actions_taken
        ]
        reasons = [
            EscalationReason(
                rule_id=item.rule_id,
                description=RULE_DESCRIPTIONS[item.rule_id],
                evidence=[_linked(e, reference, links) for e in item.evidence],
            )
            for item in decision.evidence
        ]
        questions = _dedupe(
            [
                *open_questions,
                *self._derived_questions(request, decision, auth, actions_taken, authenticated),
            ]
        )
        return HandoffPacket(
            handoff_id=self._ids(created_at),
            created_at=created_at,
            business_date=business_date(request.as_of),
            language=language,
            queue=decision.queue,
            priority=decision.priority,
            customer_ref=reference,
            auth=auth,
            request_summary=_summary(request, decision, txn, blocked, authenticated),
            reason_code=decision.reason_code,
            triggered_rules=decision.triggered_rules,
            escalation_reasons=reasons,
            verified_facts=facts,
            customer_claims=_dedupe([mask_message(claim) for claim in customer_claims]),
            collected_slots=_collected(request.slots, slot_turns or {}),
            actions_taken=actions,
            draft_case=draft,
            model_signals=self._signals(request),
            open_questions=questions,
            transcript_ref=transcript_ref,
            policy_version=decision.policy_version,
        )

    # ------------------------------------------------------------------ parts

    def _auth(self, request: PolicyRequest) -> HandoffAuth:
        session = request.session
        if session is None:
            return HandoffAuth(status=AuthStatus.UNAUTHENTICATED)
        age = request.now - session.issued_at
        idle = request.now - session.last_activity_at
        valid = age < timedelta(minutes=self._p.SESSION_MAX_AGE_MIN) and idle < timedelta(
            minutes=self._p.SESSION_IDLE_TIMEOUT_MIN
        )
        return HandoffAuth(
            status=AuthStatus.AUTHENTICATED if valid else AuthStatus.EXPIRED,
            method=session.auth_method,
            session_age_min=round(age.total_seconds() / 60, 1),
        )

    def _facts(
        self,
        request: PolicyRequest,
        decision: PolicyDecision,
        reference: str,
        blocked: dict[str, ToolResult],
    ) -> list[VerifiedFact]:
        facts: list[VerifiedFact] = []
        seen: set[tuple[str, str]] = set()

        def add(fact: str, record: Record) -> None:
            item = verified_fact(fact, record)
            if (item.source, item.record_id) not in seen:
                seen.add((item.source, item.record_id))
                facts.append(item)

        if request.customer is not None:
            # The packet never carries the customer ID: the customer record is identified by
            # its pseudonymous reference (handoff_packets.customer_id keeps the internal link).
            facts.append(
                VerifiedFact(
                    fact=f"Customer status is {request.customer.customer_status}",
                    source="customers",
                    record_id=reference,
                )
            )
        txn = _find(request.transaction_candidates, decision.transaction_id)
        if txn is not None:
            add(_transaction_fact(txn, request.as_of), txn)
            product = _product(request.products, txn.product_id)
            if product is not None:
                add(_product_fact(product, blocked), product)
        twin = _find(request.transaction_candidates, decision.duplicate_transaction_id)
        if twin is not None:
            add(f"Possible duplicate: {_transaction_fact(twin, request.as_of)}", twin)
        for product_id in (decision.card_product_id, *blocked):
            card = _product(request.products, product_id)
            if card is not None:
                add(_product_fact(card, blocked), card)
        if "ESC-04" in decision.triggered_rules and txn is not None:
            # For the agent only: fraud scores are never disclosed to the customer (ACT-06).
            facts.append(
                VerifiedFact(
                    fact=f"Fraud score {txn.fraud_score} on the disputed transaction",
                    source="transactions",
                    record_id=txn.transaction_id,
                )
            )
        if "ESC-02" in decision.triggered_rules:
            for case in request.cases:
                if case.status is not CaseStatus.DRAFT:
                    add(
                        f"Previous case {case.case_id}: {case.reason_code}, {case.status}, "
                        f"USD {case.amount_usd:,.2f}, opened {case.business_created_at:%Y-%m-%d}",
                        case,
                    )
        return facts

    def _signals(self, request: PolicyRequest) -> HandoffModelSignals:
        signals = request.signals
        calibrated = (
            self._p.DECISION_CONFIDENCE_MIN is not None
            or self._p.ESCALATION_RISK_THRESHOLD is not None
        )
        if signals.source is ModelSource.UNAVAILABLE:
            return HandoffModelSignals(source=signals.source, calibrated=calibrated)
        return HandoffModelSignals(
            source=signals.source,
            reason_code_probs=signals.reason_code_probs,
            reason_code_other=signals.reason_code_other,
            escalation_risk=signals.escalation_risk,
            model_version=signals.model_version,
            model_info=signals.model_info,
            calibrated=calibrated,
        )

    @staticmethod
    def _derived_questions(
        request: PolicyRequest,
        decision: PolicyDecision,
        auth: HandoffAuth,
        actions_taken: Sequence[ToolResult],
        authenticated: bool,
    ) -> list[str]:
        questions: list[str] = []
        if not authenticated:
            state = "the session expired" if auth.status is AuthStatus.EXPIRED else "not verified"
            questions.append(f"Verify the customer's identity ({state}).")
        else:
            # Only slots the customer has not given; a reference that matched no single
            # transaction is covered by the engine's notes instead.
            missing = [
                slot
                for slot in (SlotName.TRANSACTION_REF, SlotName.REASON_CODE)
                if getattr(request.slots, slot.value) is None
            ]
            if decision.reason_code is not None:
                missing += missing_required_slots(decision.reason_code, request.slots)
            questions += [SLOT_QUESTIONS[slot] for slot in missing]
        for result in actions_taken:
            if result.status is not ToolStatus.SUCCESS or not result.verified:
                questions.append(
                    f"{result.action} could not be verified ({result.error or result.status}): "
                    "check its result in the system of record."
                )
        questions += [NOTE_QUESTIONS.get(note, f"Review: {note}.") for note in decision.notes]
        return questions


# ---------------------------------------------------------------------- helpers


def _find(
    records: Sequence[TransactionRecord], transaction_id: str | None
) -> TransactionRecord | None:
    return next((t for t in records if t.transaction_id == transaction_id), None)


def _product(records: Sequence[ProductRecord], product_id: str | None) -> ProductRecord | None:
    return next((p for p in records if p.product_id == product_id), None)


def _blocked_cards(actions_taken: Sequence[ToolResult]) -> dict[str, ToolResult]:
    """Cards blocked and verified by ACT-03 in this turn, by product ID."""
    return {
        result.record_id: result
        for result in actions_taken
        if result.action is ActionId.BLOCK_CARD
        and result.status is ToolStatus.SUCCESS
        and result.verified
        and result.record_id is not None
    }


def _money(amount: Decimal, currency: str) -> str:
    return f"{currency} {amount:,.2f}"  # COM-08: ISO code first, never "$"


def _transaction_fact(txn: TransactionRecord, as_of: datetime) -> str:
    merchant = f" at {txn.merchant_name}" if txn.merchant_name else ""
    age = transaction_age_days(txn.transaction_date, as_of)
    days = "1 day" if age == 1 else f"{age} days"
    return (
        f"{txn.transaction_type} of {_money(txn.amount, txn.currency)}{merchant} on "
        f"{txn.transaction_date:%Y-%m-%d}, status {txn.transaction_status}, "
        f"{days} before the business date"
    )


def _product_fact(product: ProductRecord, blocked: dict[str, ToolResult]) -> str:
    last4 = product.product_number_masked[-4:]
    kind = "Card" if product.product_type in CARD_PRODUCT_TYPES else product.product_type
    block = blocked.get(product.product_id)
    if block is None:
        return f"{kind} ending {last4} is {product.product_status}"
    when = (
        f" at {block.completed_at.astimezone(UTC):%Y-%m-%d %H:%M} UTC" if block.completed_at else ""
    )
    return f"{kind} ending {last4} is Blocked (blocked by ACT-03{when})"


def _summary(
    request: PolicyRequest,
    decision: PolicyDecision,
    txn: TransactionRecord | None,
    blocked: dict[str, ToolResult],
    authenticated: bool,
) -> str:
    """Templated summary from verified data: the request, the triggers, the actions."""
    reason = decision.reason_code
    if txn is not None:
        what = REASON_NAMES[reason] if reason else f"Dispute about a {txn.transaction_type}"
        merchant = f" at {txn.merchant_name}" if txn.merchant_name else ""
        request_part = (
            f"{what} of {_money(txn.amount, txn.currency)}{merchant} "
            f"({txn.transaction_date:%Y-%m-%d})"
        )
    elif not authenticated:
        request_part = "Customer not authenticated"
    else:
        what = REASON_NAMES[reason] if reason else "Customer contact"
        request_part = f"{what}; no transaction identified"
    triggers = ", ".join(
        f"{RULE_NAMES[rule].lower()} ({rule})" for rule in decision.triggered_rules
    )
    parts = [request_part, triggers]
    for product_id in blocked:
        product = _product(request.products, product_id)
        last4 = product.product_number_masked[-4:] if product else product_id
        parts.append(f"card ****{last4} blocked")
    return "; ".join(parts) + "."


def _linked(evidence: Evidence, reference: str, links: dict[str, list[str]]) -> Evidence:
    """Evidence as shown to the agent: the customer by reference, and the claims behind it."""
    update: dict[str, object] = {}
    if evidence.source == "customers":
        update["record_id"] = reference  # the customer is named only by the pseudonym
    if evidence.kind in (EvidenceKind.SLOT, EvidenceKind.FLAG) and evidence.name in links:
        update["claims"] = links[evidence.name]
    return evidence.model_copy(update=update) if update else evidence


def _slot_value(value: object) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, TransactionRef):
        parts = value.model_dump(exclude_none=True)
        return "; ".join(f"{key}={item}" for key, item in parts.items())
    return str(value)


def _collected(slots: Slots, turns: Mapping[SlotName, int]) -> list[CollectedSlot]:
    """Slots the customer gave, in the order of policy §10; never ``confirmation``."""
    return [
        CollectedSlot(
            name=slot,
            value=mask_message(_slot_value(value)),
            turn_index=turns.get(slot),
        )
        for slot in SlotName
        if slot is not SlotName.CONFIRMATION and (value := getattr(slots, slot.value)) is not None
    ]


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(item for item in items if item.strip()))
