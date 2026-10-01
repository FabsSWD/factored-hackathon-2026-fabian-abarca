"""Shared data contracts between backend modules.

Every module exchanges data only through these models. Rule and action identifiers
(GATE-xx, ESC-xx, ACT-xx, DATA-xx, COM-xx) refer to docs/dispute-policy.md.

Two structural guarantees live here rather than in each module:

- Contracts that feed the Policy Engine or the LLM carry no field listed in DATA-01/DATA-02,
  and every model forbids extra fields, so such data cannot be attached by accident.
- Invariants that the policy states about outputs (for example, a RESOLVE always has a
  T1 or T2 tier) are validated, so a module cannot emit an inconsistent decision.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, NaiveDatetime, model_validator

# ---------------------------------------------------------------------------
# Base and shared field types
# ---------------------------------------------------------------------------


class Contract(BaseModel):
    """Base for every contract: unknown fields are rejected and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
PROBABILITY_SUM_TOLERANCE = 0.01
"""A full distribution sums to 1 within this tolerance (Kev rounds to four decimals)."""
NonEmptyStr = Annotated[str, Field(min_length=1)]
PositiveAmount = Annotated[Decimal, Field(gt=0, allow_inf_nan=False)]
NonNegativeAmount = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonNegativeFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
CurrencyCode = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]
RuleId = Annotated[str, Field(pattern=r"^(GATE|ESC)-\d{2}$")]
MaskedProductNumber = Annotated[str, Field(pattern=r"^\*{0,4}\d{4}$")]
"""COM-06: only the last four digits, optionally preceded by up to four asterisks."""

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class Outcome(StrEnum):
    """Policy §9. Precedence: REFUSE > ESCALATE > INFORM > CLARIFY > RESOLVE."""

    RESOLVE = "RESOLVE"
    CLARIFY = "CLARIFY"
    INFORM = "INFORM"
    ESCALATE = "ESCALATE"
    REFUSE = "REFUSE"


class ReasonCode(StrEnum):
    """Policy §3."""

    UNRECOGNIZED = "RC_UNRECOGNIZED"
    DUPLICATE = "RC_DUPLICATE"
    INCORRECT_AMOUNT = "RC_INCORRECT_AMOUNT"
    NOT_RECEIVED = "RC_NOT_RECEIVED"
    FEE = "RC_FEE"


class Tier(StrEnum):
    """Policy §6."""

    T1 = "T1"
    T2 = "T2"
    T3 = "T3"


class Language(StrEnum):
    """Supported conversation languages (GATE-01, COM-01)."""

    ES = "es"
    PT = "pt"


class ActionId(StrEnum):
    """Policy §8. ACT-06 (prohibited actions) is intentionally absent: it is never authorized."""

    READ_OWN_DATA = "ACT-01"
    CREATE_CASE = "ACT-02"
    BLOCK_CARD = "ACT-03"
    RECORD_CREDIT_FLAG = "ACT-04"
    TRANSFER_TO_HUMAN = "ACT-05"


WRITE_ACTIONS: frozenset[ActionId] = frozenset({ActionId.CREATE_CASE, ActionId.BLOCK_CARD})
"""Actions that require explicit confirmation (COM-03) and read-back verification."""


class Queue(StrEnum):
    """Escalation routes named in policy §7."""

    DISPUTES = "disputes"
    FRAUD = "fraud"
    SECURITY_REVIEW = "security_review"


class Priority(StrEnum):
    NORMAL = "normal"
    HIGH = "high"


class ProvisionalCreditFlag(StrEnum):
    """Policy §6. For T3 the flag is decided by a human, so it is absent (None)."""

    ELIGIBLE = "eligible"
    REQUIRES_REVIEW = "requires_review"


class SlotName(StrEnum):
    """Policy §10, in the order the system asks for them."""

    TRANSACTION_REF = "transaction_ref"
    REASON_CODE = "reason_code"
    CARD_IN_POSSESSION = "card_in_possession"
    SHARED_CREDENTIALS = "shared_credentials"
    DUPLICATE_REF = "duplicate_ref"
    EXPECTED_AMOUNT = "expected_amount"
    EXPECTED_DELIVERY_DATE = "expected_delivery_date"
    MERCHANT_CONTACTED = "merchant_contacted"
    FEE_REF = "fee_ref"
    CONFIRMATION = "confirmation"


class ClarifyTarget(StrEnum):
    """What a CLARIFY turn asks for: a §10 slot, the language (GATE-01), or
    authentication (GATE-02)."""

    TRANSACTION_REF = "transaction_ref"
    REASON_CODE = "reason_code"
    CARD_IN_POSSESSION = "card_in_possession"
    SHARED_CREDENTIALS = "shared_credentials"
    DUPLICATE_REF = "duplicate_ref"
    EXPECTED_AMOUNT = "expected_amount"
    EXPECTED_DELIVERY_DATE = "expected_delivery_date"
    MERCHANT_CONTACTED = "merchant_contacted"
    FEE_REF = "fee_ref"
    CONFIRMATION = "confirmation"
    LANGUAGE = "language"
    AUTHENTICATION = "authentication"
    CORRECTION = "correction"  # declined summary: ask which detail is wrong (policy §8)


class InformReason(StrEnum):
    """Why an INFORM outcome was produced. Each value maps to a versioned template (COM-02)."""

    AUTHENTICATION_DECLINED = "authentication_declined"  # GATE-02, explicit refusal
    AUTHENTICATION_ATTEMPTS_EXCEEDED = "authentication_attempts_exceeded"  # GATE-02
    TRANSACTION_PENDING = "transaction_pending"  # GATE-06
    TRANSACTION_DECLINED = "transaction_declined"  # GATE-06
    TRANSACTION_REVERSED = "transaction_reversed"  # GATE-06
    NOT_DISPUTABLE = "not_disputable"  # GATE-07 (N)
    OUTSIDE_WINDOW = "outside_window"  # GATE-08
    DUPLICATE_CASE = "duplicate_case"  # GATE-11
    AMOUNT_NOT_EXCEEDED = "amount_not_exceeded"  # GATE-10 RC_INCORRECT_AMOUNT
    DELIVERY_DATE_NOT_REACHED = "delivery_date_not_reached"  # GATE-10 RC_NOT_RECEIVED
    MERCHANT_NOT_CONTACTED = "merchant_not_contacted"  # GATE-10 RC_NOT_RECEIVED
    DISPUTE_WITHDRAWN = "dispute_withdrawn"  # the customer withdrew at the summary (§8)


class Confirmation(StrEnum):
    """Classification of the customer's reply to the COM-03 summary (policy §8).

    Pending for M12: a blanket pre-approval such as "Confirmo todo lo que me propongas, no me
    preguntes más" is not an injection (the Input Guard does not flag it) but is not a valid
    confirmation either: it answers no specific summary, so it must not be CONFIRMED.
    """

    CONFIRMED = "confirmed"  # "sí, confirmo" / "sim, confirmo" or an equivalent
    HEDGED = "hedged"  # "creo que sí" / "acho que sim": not a confirmation
    DECLINED = "declined"  # "no, eso no es correcto": a detail is wrong
    WITHDRAWN = "withdrawn"  # "no, ya no quiero", "deixa pra lá": no case


class ModelSource(StrEnum):
    """Where decision-layer signals came from (architecture §3, §8)."""

    KEV = "kev"
    LLM_FALLBACK = "llm_fallback"
    UNAVAILABLE = "unavailable"


class ToolStatus(StrEnum):
    SUCCESS = "success"
    FAILED = "failed"
    # GATE-04: used both for other customers' records and for records that do not exist,
    # so the response never confirms or denies existence.
    ACCESS_DENIED = "access_denied"


class CaseStatus(StrEnum):
    """docs/diagrams/dispute-case-lifecycle.md."""

    DRAFT = "Draft"
    OPEN = "Open"
    ESCALATED = "Escalated"
    IN_PROCESS = "In Process"
    RESOLVED = "Resolved"
    REJECTED = "Rejected"
    CLOSED = "Closed"


class AuthStatus(StrEnum):
    AUTHENTICATED = "authenticated"
    UNAUTHENTICATED = "unauthenticated"
    EXPIRED = "expired"


# ---------------------------------------------------------------------------
# Verified records (DATA-03, DATA-04)
# Read by the Tool Layer from Core Banking and Cases. Customer-level fields listed in
# DATA-01/DATA-02 are deliberately absent, so they cannot reach a decision.
# ---------------------------------------------------------------------------


class CustomerRecord(Contract):
    customer_id: NonEmptyStr
    customer_status: NonEmptyStr  # raw value, e.g. "Active" (GATE-03)


class ProductRecord(Contract):
    product_id: NonEmptyStr
    customer_id: NonEmptyStr
    product_type: NonEmptyStr  # raw value, e.g. "Tarjeta Crédito"
    product_number_masked: MaskedProductNumber
    currency: CurrencyCode
    product_status: NonEmptyStr  # raw value, e.g. "Active", "Blocked" (GATE-09)


class TransactionRecord(Contract):
    transaction_id: NonEmptyStr
    customer_id: NonEmptyStr
    product_id: NonEmptyStr
    transaction_type: NonEmptyStr  # raw value (policy §4, open question #1)
    transaction_status: NonEmptyStr  # raw value (GATE-06)
    # Naive local time of the dataset, on the same clock as PolicyRequest.as_of (policy §15).
    transaction_date: NaiveDatetime
    amount: PositiveAmount
    currency: CurrencyCode
    # USD equivalent (glossary): amount_usd, or daily_exchange_rates when null.
    # The Tool Layer resolves it; None means it could not be established.
    amount_usd: NonNegativeAmount | None = None
    merchant_name: str | None = None
    fraud_score: Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)] | None = None


class CaseRecord(Contract):
    case_id: NonEmptyStr
    customer_id: NonEmptyStr
    transaction_id: NonEmptyStr
    reason_code: ReasonCode
    status: CaseStatus
    tier: Tier
    amount: PositiveAmount
    currency: CurrencyCode
    amount_usd: NonNegativeAmount
    provisional_credit_flag: ProvisionalCreditFlag | None = None
    created_at: AwareDatetime  # real time, for audit
    # Business clock (naive, like PolicyRequest.as_of) at creation; used by ESC-02.
    business_created_at: NaiveDatetime


class VerifiedFact(Contract):
    """DATA-04: every verified fact names its source table and record ID."""

    fact: NonEmptyStr
    source: NonEmptyStr
    record_id: NonEmptyStr


# ---------------------------------------------------------------------------
# Conversation inputs: slots, customer-statement flags, model signals
# ---------------------------------------------------------------------------


class TransactionRef(Contract):
    """Candidate reference to a transaction (policy §10, slot ``transaction_ref``).

    Either a ``transaction_id``, or any of date, amount and merchant for the Tool Layer
    to resolve against the session customer's own transactions.

    For M8/M9: GATE-05 matches deterministically. A ``transaction_id`` proposed by the model is
    accepted only if it is consistent with the date, amount and merchant the customer gave;
    otherwise the reference counts as ambiguous and the outcome is CLARIFY. The currency the
    customer names ("dólares") is not extracted: policy §10 matches on date (±1 day), amount
    and merchant. If M8 needs it to separate same-amount candidates, add it here then.
    """

    transaction_id: NonEmptyStr | None = None
    transaction_date: date | None = None
    amount: PositiveAmount | None = None
    merchant: NonEmptyStr | None = None

    @model_validator(mode="after")
    def _not_empty(self) -> Self:
        if (
            self.transaction_id is None
            and self.transaction_date is None
            and self.amount is None
            and self.merchant is None
        ):
            raise ValueError("a transaction reference needs at least one field")
        return self


class Slots(Contract):
    """Slot values (policy §10). Values are candidates until the Policy Engine validates them.

    ``None`` means the slot has not been filled.
    """

    transaction_ref: TransactionRef | None = None
    reason_code: ReasonCode | None = None
    card_in_possession: bool | None = None
    shared_credentials: bool | None = None
    duplicate_ref: NonEmptyStr | None = None
    expected_amount: PositiveAmount | None = None
    expected_delivery_date: date | None = None
    merchant_contacted: bool | None = None
    fee_ref: NonEmptyStr | None = None
    confirmation: Confirmation | None = None


class ConversationFlags(Contract):
    """Customer statements that fire hard triggers. They are claims (DATA-03), not facts."""

    human_requested: bool = False  # ESC-05
    account_takeover_reported: bool = False  # ESC-03 (unknown login, lost phone, ...)
    legal_or_vulnerability: bool = False  # ESC-06
    authentication_declined: bool = False  # GATE-02


class ModelSignals(Contract):
    """Decision-layer output (architecture §3). Informative only; never a decision.

    How the Policy Engine may use these signals (M8):

    - ``source`` says where they came from: ``kev`` (the decision model), ``llm_fallback``
      (derived from the current message's extraction when Kev is unavailable: 0/1 values,
      deliberately uncalibrated; it is the baseline Kev is evaluated against in M18), or
      ``unavailable`` (all empty: treat as uncertainty, ESC-11).
    - As served, Kev's ``ambiguity`` and ``escalation_risk`` sit near 0.5 (0.51-0.60 on the
      verified cases). They must not influence any decision until M7 recalibrates the
      temperature per question on the validation split; until then the ESC-11 thresholds stay
      null.
    - ``reason_code_probs`` is informative: it never replaces the reason code the customer
      states and confirms. ``reason_code_other`` is the probability of a reason outside the
      five codes; with it the distribution is complete (never renormalized).
    """

    source: ModelSource
    model_version: NonEmptyStr | None = None
    # Serving details reported by the model (Kev: run, release_date), for the handoff (§13).
    model_info: dict[NonEmptyStr, str] = Field(default_factory=dict)
    reason_code_probs: dict[ReasonCode, Probability] = Field(default_factory=dict)
    reason_code_other: Probability | None = None
    ambiguity: Probability | None = None
    escalation_risk: Probability | None = None
    manipulation: Probability | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = sum(self.reason_code_probs.values()) + (self.reason_code_other or 0.0)
        if total > 1.0 + PROBABILITY_SUM_TOLERANCE:
            raise ValueError(f"reason code probabilities must sum to at most 1 (got {total})")
        has_values = bool(self.reason_code_probs) or any(
            value is not None
            for value in (
                self.reason_code_other,
                self.ambiguity,
                self.escalation_risk,
                self.manipulation,
            )
        )
        if self.source is ModelSource.UNAVAILABLE and has_values:
            raise ValueError("signals from an unavailable source must be empty")
        return self

    @property
    def top_reason_code(self) -> tuple[ReasonCode, float] | None:
        if not self.reason_code_probs:
            return None
        code = max(self.reason_code_probs, key=lambda key: self.reason_code_probs[key])
        return code, self.reason_code_probs[code]


class InputGuardResult(Contract):
    """Input Guard output for one message (ESC-13)."""

    flagged: bool
    strikes: NonNegativeInt
    pattern_id: NonEmptyStr | None = None
    escalate_security: bool = False

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.flagged and self.pattern_id is None:
            raise ValueError("a flagged message must name the pattern that matched")
        if not self.flagged and self.pattern_id is not None:
            raise ValueError("pattern_id is only set when the message is flagged")
        if self.escalate_security and self.strikes == 0:
            raise ValueError("escalate_security requires at least one strike")
        return self


class ExtractionResult(Contract):
    """LLM Adapter output for one customer message."""

    detected_language: Annotated[str, Field(pattern=r"^[a-z]{2}$")] | None = None
    language_ambiguous: bool = False
    slots: Slots = Field(default_factory=Slots)
    flags: ConversationFlags = Field(default_factory=ConversationFlags)
    customer_claims: list[NonEmptyStr] = Field(default_factory=list)


class LLMTransaction(Contract):
    """DATA-01: the only transaction fields the language model may receive."""

    transaction_ref: NonEmptyStr
    transaction_date: date
    amount: PositiveAmount
    currency: CurrencyCode
    merchant_name: str | None = None
    transaction_status: NonEmptyStr


class LLMContext(Contract):
    """DATA-01: everything the language model may receive besides the customer's message.

    No document numbers, dates of birth, addresses, phone numbers, emails or full names.
    """

    customer_ref: NonEmptyStr  # pseudonymous reference, never the customer_id
    language: Language | None = None
    masked_products: list[MaskedProductNumber] = Field(default_factory=list)
    transactions: list[LLMTransaction] = Field(default_factory=list)
    pending_slot: SlotName | None = None
    # Transaction refs listed to the customer in the previous turn (choose_transaction). A
    # transaction_id proposed by the model is kept only if it is one of these or appears
    # literally in the customer's message.
    shown_candidates: list[NonEmptyStr] = Field(default_factory=list)
    # The business date (as_of), so the model can resolve "ayer" and the code can complete the
    # year of a partial date (policy §15, business clock).
    business_date: date | None = None


# ---------------------------------------------------------------------------
# Session and counters
# ---------------------------------------------------------------------------


class SessionContext(Contract):
    """Identity Service output. ``customer_id`` comes only from the validated token (GATE-04)."""

    session_id: NonEmptyStr
    customer_id: NonEmptyStr
    auth_method: NonEmptyStr
    issued_at: AwareDatetime
    last_activity_at: AwareDatetime

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.last_activity_at < self.issued_at:
            raise ValueError("last_activity_at cannot precede issued_at")
        return self


class ConversationCounters(Contract):
    """Per-conversation counters kept by the Orchestrator."""

    clarifications_by_slot: dict[ClarifyTarget, NonNegativeInt] = Field(default_factory=dict)
    total_clarifications: NonNegativeInt = 0
    language_clarifications: NonNegativeInt = 0  # GATE-01 allows one
    # GATE-02: authentication requests, bounded by AUTH_MAX_ATTEMPTS and kept apart from slot
    # clarifications so they do not count toward MAX_TOTAL_CLARIFICATIONS.
    authentication_attempts: NonNegativeInt = 0
    injection_strikes: NonNegativeInt = 0  # ESC-13
    unrecognized_transactions: NonNegativeInt = 0  # ESC-03 batch count
    unresolved_contradiction: bool = False  # ESC-09 (claim contradicts verified facts)
    # GATE-10 RC_DUPLICATE: the "another reason?" question was already asked (exactly once).
    duplicate_reason_reasked: bool = False


# ---------------------------------------------------------------------------
# Tool Layer results
# ---------------------------------------------------------------------------


class ToolResult(Contract):
    """Result of one Tool Layer write action (ACT-02, ACT-03, ACT-05).

    For write actions with read-back verification, ``success`` implies ``verified`` and
    ``verified`` implies ``success`` (policy §8, COM-04).
    """

    action: ActionId
    status: ToolStatus
    verified: bool
    attempts: Annotated[int, Field(ge=1)]
    idempotency_key: NonEmptyStr | None = None
    record_id: NonEmptyStr | None = None
    detail: str | None = None
    error: str | None = None
    completed_at: AwareDatetime | None = None  # real time, set by the Tool Layer

    @model_validator(mode="after")
    def _check(self) -> Self:
        if self.action in WRITE_ACTIONS and self.verified != (self.status is ToolStatus.SUCCESS):
            raise ValueError("a write action is verified if and only if it succeeded")
        if (
            self.action is not ActionId.TRANSFER_TO_HUMAN
            and self.status is ToolStatus.SUCCESS
            and self.record_id is None
        ):
            raise ValueError("a successful action must return the record it wrote")
        if self.status is ToolStatus.FAILED and not self.error:
            raise ValueError("a failed action must describe its error")
        if self.action is ActionId.CREATE_CASE and self.idempotency_key is None:
            raise ValueError("ACT-02 requires the idempotency key transaction_id + reason_code")
        return self


# ---------------------------------------------------------------------------
# Policy Engine input and output
# ---------------------------------------------------------------------------


class PolicyRequest(Contract):
    """Everything the Policy Engine needs to evaluate one disputed transaction.

    The engine is a pure function of this object: it performs no I/O and never reads the
    clock. Two clocks are passed in (policy §15, "Business date"):

    - ``now``: real time, for session age and idle time (GATE-02).
    - ``as_of``: the end of the simulated business day, for transaction age and calendar
      windows (GATE-08, ESC-02, ESC-07, RC_NOT_RECEIVED delivery date). It is naive, like
      the dataset's timestamps, which carry no time zone.
    """

    now: AwareDatetime
    as_of: NaiveDatetime
    conversation_id: NonEmptyStr

    # GATE-01
    detected_language: Annotated[str, Field(pattern=r"^[a-z]{2}$")] | None = None
    language_ambiguous: bool = False

    # GATE-02; None when there is no valid token
    session: SessionContext | None = None

    # Customer inputs
    slots: Slots = Field(default_factory=Slots)
    flags: ConversationFlags = Field(default_factory=ConversationFlags)
    signals: ModelSignals = Field(
        default_factory=lambda: ModelSignals(source=ModelSource.UNAVAILABLE)
    )
    input_guard: InputGuardResult | None = None
    counters: ConversationCounters = Field(default_factory=ConversationCounters)

    # Verified records read through the Tool Layer (only after GATE-02 passes). The engine
    # applies the matching and counting rules itself, so the Tool Layer only reads.
    customer: CustomerRecord | None = None
    ownership_violation: bool = False  # GATE-04: the Tool Layer returned access_denied
    # The pool GATE-05 and the RC_DUPLICATE rule search: the customer's transactions within
    # LATE_WINDOW_DAYS of as_of, plus any transaction the customer referenced by ID.
    transaction_candidates: list[TransactionRecord] = Field(default_factory=list)
    # The customer's products: the product of the transaction (GATE-09, ACT-03) and the cards
    # considered for ACT-03 when ESC-03 fires without an identified transaction.
    products: list[ProductRecord] = Field(default_factory=list)
    # The customer's cases (any status): GATE-11 and ESC-02.
    cases: list[CaseRecord] = Field(default_factory=list)
    tool_results: list[ToolResult] = Field(default_factory=list)  # ESC-10

    @model_validator(mode="after")
    def _check(self) -> Self:
        records_present = (
            self.customer is not None or self.transaction_candidates or self.products or self.cases
        )
        if self.session is None and records_present:
            raise ValueError("account records cannot be present without a session (GATE-02)")
        if self.session is not None:
            owner = self.session.customer_id
            owned: list[CustomerRecord | ProductRecord | TransactionRecord | CaseRecord] = [
                *self.transaction_candidates,
                *self.products,
                *self.cases,
            ]
            if self.customer is not None:
                owned.append(self.customer)
            if any(record.customer_id != owner for record in owned):
                raise ValueError("records of another customer cannot enter a request (GATE-04)")
        return self


class EvidenceKind(StrEnum):
    """Where a piece of evidence for a fired rule comes from."""

    SLOT = "slot"  # a value the customer stated (DATA-03: a claim)
    FLAG = "flag"  # a customer-statement flag (rule detector and/or LLM extraction)
    COUNTER = "counter"  # a conversation counter kept by the Orchestrator
    RECORD = "record"  # a verified record (DATA-04: source table and record ID)
    SIGNAL = "signal"  # decision-layer signals (informative)
    TOOL_RESULT = "tool_result"  # a Tool Layer action result
    INPUT_GUARD = "input_guard"  # the Input Guard's verdict
    LANGUAGE = "language"  # language detection


class Evidence(Contract):
    """One input that made a rule fire. Taken from the request, never generated."""

    kind: EvidenceKind
    name: NonEmptyStr  # e.g. "shared_credentials", "fraud_score", "injection_strikes"
    value: NonEmptyStr  # e.g. "yes", "91.5 (threshold 35)"
    origin: NonEmptyStr  # e.g. "customer statement (LLM extraction)", "Core Banking"
    source: NonEmptyStr | None = None  # table, for records
    record_id: NonEmptyStr | None = None


class RuleEvidence(Contract):
    rule_id: RuleId
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class GateResult(Contract):
    gate_id: Annotated[str, Field(pattern=r"^GATE-\d{2}$")]
    passed: bool


class PolicyDecision(Contract):
    """Policy Engine output for one disputed transaction."""

    outcome: Outcome
    policy_version: NonEmptyStr
    gates_evaluated: list[GateResult] = Field(default_factory=list)
    triggered_rules: list[RuleId] = Field(default_factory=list)
    authorized_actions: list[ActionId] = Field(default_factory=list)
    queue: Queue | None = None
    priority: Priority | None = None
    tier: Tier | None = None
    amount_usd: NonNegativeAmount | None = None
    provisional_credit_flag: ProvisionalCreditFlag | None = None
    clarify_target: ClarifyTarget | None = None
    inform_reason: InformReason | None = None
    transaction_id: NonEmptyStr | None = None
    reason_code: ReasonCode | None = None
    # What the Orchestrator needs to render the turn (templates) and the handoff (M10):
    candidate_transaction_ids: list[NonEmptyStr] = Field(default_factory=list)  # choose_transaction
    duplicate_transaction_id: NonEmptyStr | None = None  # clarify_duplicate_ref
    existing_case_id: NonEmptyStr | None = None  # duplicate_case (GATE-11)
    existing_case_status: CaseStatus | None = None
    card_product_id: NonEmptyStr | None = None  # the card ACT-03 would block
    card_already_blocked: bool = False  # card_already_blocked template
    # This CLARIFY is the single RC_DUPLICATE re-ask of the reason code: the Orchestrator sets
    # counters.duplicate_reason_reasked.
    duplicate_reason_reask: bool = False
    # Codes for the audit record and the handoff's open_questions, e.g. "fraud_score_missing".
    notes: list[NonEmptyStr] = Field(default_factory=list)
    # Why each triggered rule fired (handoff escalation_reasons, audit).
    evidence: list[RuleEvidence] = Field(default_factory=list)

    @model_validator(mode="after")
    def _check(self) -> Self:
        escalate = self.outcome is Outcome.ESCALATE
        if escalate != (self.queue is not None and self.priority is not None):
            raise ValueError("queue and priority are set if and only if the outcome is ESCALATE")
        if escalate and not any(rule.startswith("ESC-") for rule in self.triggered_rules):
            raise ValueError("an ESCALATE outcome must name the escalation rule that fired")
        if (self.outcome is Outcome.CLARIFY) != (self.clarify_target is not None):
            raise ValueError("clarify_target is set if and only if the outcome is CLARIFY")
        if (self.outcome is Outcome.INFORM) != (self.inform_reason is not None):
            raise ValueError("inform_reason is set if and only if the outcome is INFORM")
        if self.outcome is Outcome.RESOLVE and self.tier not in (Tier.T1, Tier.T2):
            raise ValueError("RESOLVE requires tier T1 or T2 (policy §6)")
        if ActionId.CREATE_CASE in self.authorized_actions:
            if self.outcome is not Outcome.RESOLVE:
                raise ValueError("ACT-02 can only be authorized with a RESOLVE outcome")
            if self.transaction_id is None or self.reason_code is None:
                raise ValueError("ACT-02 needs the transaction and the reason code")
        if (ActionId.RECORD_CREDIT_FLAG in self.authorized_actions) != (
            ActionId.CREATE_CASE in self.authorized_actions
        ):
            raise ValueError("ACT-04 is authorized together with ACT-02 and never alone")
        if self.outcome is Outcome.REFUSE and self.authorized_actions:
            raise ValueError("a REFUSE outcome authorizes no action")
        if (ActionId.BLOCK_CARD in self.authorized_actions) != (self.card_product_id is not None):
            raise ValueError("ACT-03 is authorized if and only if a card product is named")
        expected_flag = (
            {
                Tier.T1: ProvisionalCreditFlag.ELIGIBLE,
                Tier.T2: ProvisionalCreditFlag.REQUIRES_REVIEW,
            }.get(self.tier)
            if self.tier is not None
            else None
        )
        if self.provisional_credit_flag is not None and self.provisional_credit_flag != (
            expected_flag
        ):
            raise ValueError("provisional_credit_flag does not match the tier (policy §6)")
        escalation_rules = [rule for rule in self.triggered_rules if rule.startswith("ESC-")]
        if [e.rule_id for e in self.evidence] != escalation_rules:
            raise ValueError("every triggered escalation rule has its evidence, in the same order")
        return self


# ---------------------------------------------------------------------------
# Handoff packet (policy §13)
# ---------------------------------------------------------------------------


class HandoffAuth(Contract):
    status: AuthStatus
    method: NonEmptyStr | None = None
    session_age_min: NonNegativeFloat | None = None


class ActionTaken(Contract):
    """An attempted action with its verification result. Failed actions are included."""

    action: ActionId
    result: ToolStatus
    verified: bool
    detail: str | None = None
    at: AwareDatetime | None = None  # when the action finished (real time)


class DraftCase(Contract):
    transaction_ref: NonEmptyStr
    amount_usd: NonNegativeAmount | None = None
    tier: Tier | None = None
    provisional_credit_flag: ProvisionalCreditFlag | None = None


class HandoffModelSignals(Contract):
    """Informative model output shown to the agent (never a decision)."""

    source: ModelSource | None = None
    reason_code_probs: dict[ReasonCode, Probability] = Field(default_factory=dict)
    escalation_risk: Probability | None = None
    model_version: NonEmptyStr | None = None
    model_info: dict[NonEmptyStr, str] = Field(default_factory=dict)  # Kev run, release_date
    calibrated: bool = False  # false until M7 calibrates the ESC-11 thresholds


class EscalationReason(Contract):
    """A triggered rule in plain words, with the evidence that made it fire."""

    rule_id: RuleId
    description: NonEmptyStr
    evidence: Annotated[list[Evidence], Field(min_length=1)]


class HandoffPacket(Contract):
    """Policy §13. Never contains the raw transcript, only ``transcript_ref``."""

    handoff_id: Annotated[str, Field(pattern=r"^HO-\d{8}-\d{6,}$")]
    created_at: AwareDatetime
    # The business clock (policy §15): transaction ages are counted against this date, not
    # against created_at.
    business_date: date
    language: Language
    queue: Queue
    priority: Priority
    customer_ref: NonEmptyStr
    auth: HandoffAuth
    request_summary: NonEmptyStr
    reason_code: ReasonCode | None = None
    triggered_rules: Annotated[list[RuleId], Field(min_length=1)]
    escalation_reasons: list[EscalationReason] = Field(default_factory=list)
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    customer_claims: list[NonEmptyStr] = Field(default_factory=list)
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    draft_case: DraftCase | None = None
    model_signals: HandoffModelSignals = Field(default_factory=HandoffModelSignals)
    open_questions: list[NonEmptyStr] = Field(default_factory=list)
    transcript_ref: NonEmptyStr
    policy_version: NonEmptyStr

    @model_validator(mode="after")
    def _check(self) -> Self:
        if [r.rule_id for r in self.escalation_reasons] != list(self.triggered_rules):
            raise ValueError("every triggered rule has an escalation reason, in the same order")
        return self


# ---------------------------------------------------------------------------
# Trace record (architecture §9)
# ---------------------------------------------------------------------------


class ModelCall(Contract):
    provider: NonEmptyStr  # "openai" | "kev"
    model: NonEmptyStr  # the model requested (configuration)
    response_model: NonEmptyStr | None = None  # exact model version reported by the provider
    system_fingerprint: NonEmptyStr | None = None  # provider backend fingerprint, if reported
    prompt_version: NonEmptyStr | None = None
    # "sha256:" + 16 hex of the system prompt and schema, so an unversioned edit is visible.
    prompt_hash: Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{16}$")] | None = None
    # Latency reported by the model server itself; latency_ms minus this is network and
    # serialization cost.
    server_latency_ms: NonNegativeFloat | None = None
    # Serving details reported by the provider (Kev: run, release_date, temperature, dtype,
    # device), so a result can be traced to the exact model build.
    model_info: dict[NonEmptyStr, str] = Field(default_factory=dict)
    purpose: NonEmptyStr  # e.g. "extract_slots", "decision_signals"
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    latency_ms: NonNegativeFloat
    success: bool
    error: str | None = None
    # Deterministic corrections applied to the model's answer, e.g. "transaction_id_discarded".
    adjustments: list[NonEmptyStr] = Field(default_factory=list)


class TraceRecord(Contract):
    """One record per turn. Optional stages that did not run are recorded as None."""

    trace_id: NonEmptyStr
    conversation_id: NonEmptyStr
    session_id: NonEmptyStr | None = None
    turn_index: NonNegativeInt
    created_at: AwareDatetime
    language: Language | None = None
    message: str | None = None  # retention/masking policy decided in M11
    input_guard: InputGuardResult | None = None
    model_calls: list[ModelCall] = Field(default_factory=list)
    signals: ModelSignals | None = None
    decisions: list[PolicyDecision] = Field(default_factory=list)
    tool_calls: list[ToolResult] = Field(default_factory=list)
    outcome: Outcome | None = None
    handoff_id: NonEmptyStr | None = None
    stage_latencies_ms: dict[NonEmptyStr, NonNegativeFloat] = Field(default_factory=dict)
    total_latency_ms: NonNegativeFloat | None = None
    estimated_cost_usd: NonNegativeAmount | None = None
    policy_version: NonEmptyStr
    error: str | None = None


class AccessDeniedError(Exception):
    """GATE-04: raised by Tool Layer reads for records the session customer cannot access.

    The same error is raised whether the record belongs to someone else or does not exist.
    """


__all__ = [
    "WRITE_ACTIONS",
    "AccessDeniedError",
    "ActionId",
    "ActionTaken",
    "AuthStatus",
    "CaseRecord",
    "CaseStatus",
    "ClarifyTarget",
    "Confirmation",
    "ConversationCounters",
    "ConversationFlags",
    "CustomerRecord",
    "DraftCase",
    "EscalationReason",
    "Evidence",
    "EvidenceKind",
    "ExtractionResult",
    "GateResult",
    "HandoffAuth",
    "HandoffModelSignals",
    "HandoffPacket",
    "InformReason",
    "InputGuardResult",
    "LLMContext",
    "LLMTransaction",
    "Language",
    "ModelCall",
    "ModelSignals",
    "ModelSource",
    "Outcome",
    "PolicyDecision",
    "PolicyRequest",
    "Priority",
    "ProductRecord",
    "ProvisionalCreditFlag",
    "Queue",
    "ReasonCode",
    "RuleEvidence",
    "SessionContext",
    "SlotName",
    "Slots",
    "Tier",
    "ToolResult",
    "ToolStatus",
    "TraceRecord",
    "TransactionRecord",
    "TransactionRef",
    "VerifiedFact",
]
