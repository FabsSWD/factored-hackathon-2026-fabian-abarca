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

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# Base and shared field types
# ---------------------------------------------------------------------------


class Contract(BaseModel):
    """Base for every contract: unknown fields are rejected and instances are immutable."""

    model_config = ConfigDict(extra="forbid", frozen=True)


Probability = Annotated[float, Field(ge=0.0, le=1.0, allow_inf_nan=False)]
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


class InformReason(StrEnum):
    """Why an INFORM outcome was produced. Each value maps to a versioned template (COM-02)."""

    AUTHENTICATION_DECLINED = "authentication_declined"  # GATE-02
    TRANSACTION_PENDING = "transaction_pending"  # GATE-06
    TRANSACTION_DECLINED = "transaction_declined"  # GATE-06
    TRANSACTION_REVERSED = "transaction_reversed"  # GATE-06
    NOT_DISPUTABLE = "not_disputable"  # GATE-07 (N)
    OUTSIDE_WINDOW = "outside_window"  # GATE-08
    DUPLICATE_CASE = "duplicate_case"  # GATE-11
    AMOUNT_NOT_EXCEEDED = "amount_not_exceeded"  # GATE-10 RC_INCORRECT_AMOUNT
    DELIVERY_DATE_NOT_REACHED = "delivery_date_not_reached"  # GATE-10 RC_NOT_RECEIVED
    MERCHANT_NOT_CONTACTED = "merchant_not_contacted"  # GATE-10 RC_NOT_RECEIVED


class Confirmation(StrEnum):
    """Classification of the customer's reply to the COM-03 summary (policy §8)."""

    CONFIRMED = "confirmed"  # "sí, confirmo" / "sim, confirmo" or an equivalent
    HEDGED = "hedged"  # "creo que sí" / "acho que sim": not a confirmation
    DECLINED = "declined"


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
    transaction_date: AwareDatetime
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
    created_at: AwareDatetime


class DisputeHistory(Contract):
    """Aggregates for ESC-02, excluding the dispute being evaluated."""

    disputed_usd_last_30d: NonNegativeAmount
    cases_last_90d: NonNegativeInt


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
    """Decision-layer output (architecture §3). Informative only; never a decision."""

    source: ModelSource
    model_version: NonEmptyStr | None = None
    reason_code_probs: dict[ReasonCode, Probability] = Field(default_factory=dict)
    ambiguity: Probability | None = None
    escalation_risk: Probability | None = None
    manipulation: Probability | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        total = sum(self.reason_code_probs.values())
        if total > 1.0 + 1e-6:
            raise ValueError(f"reason_code_probs must sum to at most 1 (got {total})")
        has_values = bool(self.reason_code_probs) or any(
            value is not None for value in (self.ambiguity, self.escalation_risk, self.manipulation)
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
    injection_strikes: NonNegativeInt = 0  # ESC-13
    unrecognized_transactions: NonNegativeInt = 0  # ESC-03 batch count
    unresolved_contradiction: bool = False  # ESC-09 (claim contradicts verified facts)


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

    The engine is a pure function of this object: it performs no I/O and reads the time
    from ``now``, never from the clock.
    """

    now: AwareDatetime
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

    # Verified records read through the Tool Layer (only after GATE-02 passes)
    customer: CustomerRecord | None = None
    ownership_violation: bool = False  # GATE-04: the Tool Layer returned access_denied
    transaction_candidates: list[TransactionRecord] = Field(default_factory=list)  # GATE-05
    product: ProductRecord | None = None
    duplicate_candidates: list[TransactionRecord] = Field(default_factory=list)  # GATE-10
    fee_candidates: list[TransactionRecord] = Field(default_factory=list)  # GATE-10 RC_FEE
    open_cases: list[CaseRecord] = Field(default_factory=list)  # GATE-11
    dispute_history: DisputeHistory | None = None  # ESC-02
    tool_results: list[ToolResult] = Field(default_factory=list)  # ESC-10

    @model_validator(mode="after")
    def _check(self) -> Self:
        records_present = (
            self.customer is not None
            or self.transaction_candidates
            or self.product is not None
            or self.open_cases
            or self.dispute_history is not None
        )
        if self.session is None and records_present:
            raise ValueError("account records cannot be present without a session (GATE-02)")
        if self.session is not None:
            owner = self.session.customer_id
            owned: list[CustomerRecord | ProductRecord | TransactionRecord | CaseRecord] = [
                *self.transaction_candidates,
                *self.duplicate_candidates,
                *self.fee_candidates,
                *self.open_cases,
            ]
            if self.customer is not None:
                owned.append(self.customer)
            if self.product is not None:
                owned.append(self.product)
            if any(record.customer_id != owner for record in owned):
                raise ValueError("records of another customer cannot enter a request (GATE-04)")
        return self


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


class DraftCase(Contract):
    transaction_ref: NonEmptyStr
    amount_usd: NonNegativeAmount | None = None
    tier: Tier | None = None
    provisional_credit_flag: ProvisionalCreditFlag | None = None


class HandoffModelSignals(Contract):
    """Informative model output shown to the agent (never a decision)."""

    reason_code_probs: dict[ReasonCode, Probability] = Field(default_factory=dict)
    escalation_risk: Probability | None = None
    model_version: NonEmptyStr | None = None


class HandoffPacket(Contract):
    """Policy §13. Never contains the raw transcript, only ``transcript_ref``."""

    handoff_id: Annotated[str, Field(pattern=r"^HO-\d{8}-\d{6}$")]
    created_at: AwareDatetime
    language: Language
    queue: Queue
    priority: Priority
    customer_ref: NonEmptyStr
    auth: HandoffAuth
    request_summary: NonEmptyStr
    reason_code: ReasonCode | None = None
    triggered_rules: Annotated[list[RuleId], Field(min_length=1)]
    verified_facts: list[VerifiedFact] = Field(default_factory=list)
    customer_claims: list[NonEmptyStr] = Field(default_factory=list)
    actions_taken: list[ActionTaken] = Field(default_factory=list)
    draft_case: DraftCase | None = None
    model_signals: HandoffModelSignals = Field(default_factory=HandoffModelSignals)
    open_questions: list[NonEmptyStr] = Field(default_factory=list)
    transcript_ref: NonEmptyStr
    policy_version: NonEmptyStr


# ---------------------------------------------------------------------------
# Trace record (architecture §9)
# ---------------------------------------------------------------------------


class ModelCall(Contract):
    provider: NonEmptyStr  # "openai" | "kev"
    model: NonEmptyStr
    prompt_version: NonEmptyStr | None = None
    purpose: NonEmptyStr  # e.g. "extract_slots", "decision_signals"
    input_tokens: NonNegativeInt | None = None
    output_tokens: NonNegativeInt | None = None
    latency_ms: NonNegativeFloat
    success: bool
    error: str | None = None


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
    "DisputeHistory",
    "DraftCase",
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
