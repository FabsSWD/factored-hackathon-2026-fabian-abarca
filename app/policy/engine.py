"""M8 Policy Engine: the only component that decides outcomes (policy principle 1).

``evaluate`` is a pure function of a ``PolicyRequest`` and the ``PolicyConfig``: no I/O, no
clock, no models. The M17 reference labeler runs this same code on scenario specifications.

Evaluation follows policy §5 in two channels, then §9 precedence:

1. Interrupts, on every turn: ESC-03 (statements and the unrecognized batch), ESC-05, ESC-06
   and ESC-13. Once the session is authenticated, also ESC-10 (a failed write action) and the
   unresolved-contradiction branch of ESC-09.
2. Gates GATE-01..GATE-11 in order, stopping at the first that does not pass. ESC-03 (shared
   credentials), ESC-07, ESC-08, ESC-12 and ESC-14 are gate outcomes. The reason code is asked
   (§10) after GATE-06 and before GATE-07, which needs it.
3. Record-dependent triggers ESC-01, ESC-02 and ESC-04, only when every gate passes.
4. The COM-03 confirmation, when every gate passes and no trigger fired.
5. ESC-09 replaces a CLARIFY once the clarification limits are reached, and the soft ESC-11 is
   considered only when no hard rule decided the outcome (the candidate is CLARIFY or RESOLVE)
   and the session is authenticated.

Contract for the Orchestrator (M12):

- ``detected_language`` is the conversation's language for the turn (COM-01), not a per-message
  guess that a short reply such as "C2" would make ambiguous.
- ``counters`` count past turns. A CLARIFY counts toward the limits except when the target is
  ``language`` or ``authentication`` (their own limits) and except the first presentation of
  the summary (target ``confirmation`` while ``slots.confirmation`` is None).
- ``slots.confirmation`` is the reply to the question pending in the previous turn: the ACT-02
  summary, or the duplicate question of RC_DUPLICATE. On a yes to the duplicate question, fill
  ``duplicate_ref`` with ``duplicate_transaction_id`` and clear ``confirmation``; clear it as
  well whenever a slot changes after the summary, so a stale yes never confirms a new summary.
- When a decision has ``duplicate_reason_reask``, set ``counters.duplicate_reason_reasked``:
  RC_DUPLICATE re-asks the reason exactly once.
- ACT-03 is authorized whenever its §8 conditions hold; the Orchestrator asks its own
  confirmation first and does not offer it again once the customer declined or it ran.
- GATE-04 is per record: when the customer references several records, evaluate each one in
  its own request, so a record of another customer is refused while their own is handled.
- Any unexpected exception from the engine (for example ``ValueError`` on a value outside the
  data contract) or from the Tool Layer becomes a safe reply: a handoff with the
  ``tool_failure`` template and an audit event, never an HTTP error with a stack trace.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from app.config import PolicyConfig
from app.contracts import (
    WRITE_ACTIONS,
    ActionId,
    CaseRecord,
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    Evidence,
    EvidenceKind,
    GateResult,
    InformReason,
    Language,
    ModelSource,
    Outcome,
    PolicyDecision,
    PolicyRequest,
    Priority,
    ProductRecord,
    ProvisionalCreditFlag,
    Queue,
    ReasonCode,
    RuleEvidence,
    Tier,
    ToolStatus,
    TransactionRecord,
)
from app.policy import evidence as ev
from app.policy.clock import (
    business_date,
    transaction_age_days,
    transaction_within,
    within_window,
)
from app.policy.matching import disputed_of, duplicate_twins, match_transaction, nearest_twin
from app.policy.rules import (
    ACCOUNT_INITIATED_TYPES,
    ACTION_NAMES,
    DISPUTABILITY,
    GATE_NAMES,
    QUEUE_RANK,
    ROUTES,
    RULE_NAMES,
    Mark,
)
from app.storage.data_contract import CARD_PRODUCT_TYPES

SUPPORTED_LANGUAGES = frozenset(language.value for language in Language)
LANGUAGE_CLARIFICATIONS_MAX = 1  # GATE-01: one clarification, then ESC-12
VELOCITY_AMOUNT_DAYS = 30  # ESC-02 window of the disputed total
VELOCITY_COUNT_DAYS = 90  # ESC-02 window of the case count
ELIGIBLE_PRODUCT_STATUSES = frozenset({"Active", "Blocked"})  # GATE-09
_GATE06_INFORM = {
    "Pending": InformReason.TRANSACTION_PENDING,
    "Declined": InformReason.TRANSACTION_DECLINED,
    "Reversed": InformReason.TRANSACTION_REVERSED,
}


@dataclass(frozen=True)
class _Verdict:
    """The candidate outcome of the gate channel or of the confirmation step."""

    outcome: Outcome
    clarify_target: ClarifyTarget | None = None
    inform_reason: InformReason | None = None
    rule: str | None = None  # the escalation rule a gate fired
    # Whether a CLARIFY is a clarification turn for MAX_CLARIFICATION_TURNS and
    # MAX_TOTAL_CLARIFICATIONS (ESC-09).
    counts: bool = True
    candidates: tuple[str, ...] = ()
    existing_case: CaseRecord | None = None
    reask: bool = False  # the RC_DUPLICATE re-ask of the reason code
    evidence: tuple[Evidence, ...] = ()  # why the gate's escalation rule fired


def _clarify(
    target: ClarifyTarget, *, counts: bool = True, candidates: tuple[str, ...] = ()
) -> _Verdict:
    return _Verdict(Outcome.CLARIFY, clarify_target=target, counts=counts, candidates=candidates)


def _inform(reason: InformReason, existing_case: CaseRecord | None = None) -> _Verdict:
    return _Verdict(Outcome.INFORM, inform_reason=reason, existing_case=existing_case)


def _escalate(rule: str, *evidence: Evidence) -> _Verdict:
    return _Verdict(Outcome.ESCALATE, rule=rule, evidence=evidence)


@dataclass
class _State:
    """What one evaluation has established so far."""

    gates: list[GateResult] = field(default_factory=list)
    fired: dict[str, tuple[Queue, Priority]] = field(default_factory=dict)
    evidence: dict[str, list[Evidence]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    authenticated: bool = False
    transaction: TransactionRecord | None = None  # identified by GATE-05
    product: ProductRecord | None = None  # the transaction's product
    disputed: TransactionRecord | None = None  # the transaction the case would be about
    twin: TransactionRecord | None = None  # RC_DUPLICATE: the other charge of the pair

    def gate(self, gate_id: str, passed: bool) -> bool:
        self.gates.append(GateResult(gate_id=gate_id, passed=passed))
        return passed

    def fire(self, rule: str, evidence: list[Evidence], queue: Queue | None = None) -> None:
        if not evidence:
            raise ValueError(f"{rule} fired without evidence")
        default_queue, priority = ROUTES[rule]
        self.fired.setdefault(rule, (queue or default_queue, priority))
        known = self.evidence.setdefault(rule, [])
        known.extend(item for item in evidence if item not in known)

    def note(self, code: str) -> None:
        self.notes.append(code)


class DeterministicPolicyEngine:
    """Implements ``app.interfaces.PolicyEngine`` for docs/dispute-policy.md."""

    def __init__(self, config: PolicyConfig) -> None:
        self._config = config
        self._p = config.parameters

    @property
    def policy_version(self) -> str:
        return self._config.policy_version

    # ------------------------------------------------------------------ evaluate

    def evaluate(self, request: PolicyRequest) -> PolicyDecision:
        state = _State()
        self._interrupts(request, state)
        verdict = self._gates(request, state)
        if state.authenticated:
            self._authenticated_interrupts(request, state)

        tier: Tier | None = None
        amount_usd: Decimal | None = None
        if state.disputed is not None:
            amount_usd = state.disputed.amount_usd
            tier = self._tier(amount_usd)
            if amount_usd is None:
                state.note("amount_usd_missing")
        if verdict is None:
            assert state.disputed is not None and tier is not None
            self._record_triggers(request, state, state.disputed, tier)
            if not state.fired:
                verdict = self._confirmation(request)
        if verdict is not None and verdict.rule is not None:
            queue = self._esc14_queue(request, state) if verdict.rule == "ESC-14" else None
            state.fire(verdict.rule, list(verdict.evidence), queue)

        if verdict is not None and verdict.outcome is Outcome.CLARIFY and verdict.counts:
            exhausted = self._clarifications_exhausted(request, verdict)
            if exhausted:
                state.fire("ESC-09", exhausted)
        if (
            state.authenticated
            and not state.fired
            and verdict is not None
            and verdict.outcome in (Outcome.CLARIFY, Outcome.RESOLVE)
        ):
            uncertainty = self._model_uncertain(request)
            if uncertainty:
                state.fire("ESC-11", uncertainty)

        assert verdict is not None or state.fired
        return self._decision(request, state, verdict, tier, amount_usd)

    # ------------------------------------------------------------------ channel 1

    def _interrupts(self, request: PolicyRequest, state: _State) -> None:
        p, counters, flags = self._p, request.counters, request.flags
        takeover: list[Evidence] = []
        if flags.account_takeover_reported:
            takeover.append(ev.flag("account_takeover_reported"))
        if counters.unrecognized_transactions >= p.UNRECOGNIZED_BATCH_MAX:
            takeover.append(
                ev.counter(
                    "unrecognized_transactions",
                    f"{counters.unrecognized_transactions} (limit {p.UNRECOGNIZED_BATCH_MAX})",
                )
            )
        if takeover:
            state.fire("ESC-03", takeover)
        if flags.human_requested:
            state.fire("ESC-05", [ev.flag("human_requested")])
        if flags.legal_or_vulnerability:
            state.fire("ESC-06", [ev.flag("legal_or_vulnerability")])
        guard = request.input_guard
        strikes = max(counters.injection_strikes, guard.strikes if guard is not None else 0)
        if strikes >= p.INJECTION_STRIKES_MAX or (guard is not None and guard.escalate_security):
            manipulation = [
                ev.counter("injection_strikes", f"{strikes} (limit {p.INJECTION_STRIKES_MAX})")
            ]
            if guard is not None and guard.pattern_id is not None:
                manipulation.append(
                    ev.other(
                        EvidenceKind.INPUT_GUARD, "pattern_id", guard.pattern_id, "Input Guard"
                    )
                )
            state.fire("ESC-13", manipulation)

    def _authenticated_interrupts(self, request: PolicyRequest, state: _State) -> None:
        failures = [
            ev.other(
                EvidenceKind.TOOL_RESULT,
                result.action.value,
                f"{result.status}" + (f": {result.error}" if result.error else ""),
                "Tool Layer",
            )
            for result in request.tool_results
            if result.action in WRITE_ACTIONS
            and (result.status is not ToolStatus.SUCCESS or not result.verified)
        ]
        if failures:
            state.fire("ESC-10", failures)
        if request.counters.unresolved_contradiction:
            state.fire("ESC-09", [ev.counter("unresolved_contradiction", "true")])

    # ------------------------------------------------------------------ channel 2

    def _gates(self, request: PolicyRequest, state: _State) -> _Verdict | None:
        p = self._p
        # GATE-01
        language_ok = (
            request.detected_language in SUPPORTED_LANGUAGES and not request.language_ambiguous
        )
        if not state.gate("GATE-01", language_ok):
            if request.counters.language_clarifications >= LANGUAGE_CLARIFICATIONS_MAX:
                detected = request.detected_language or "unknown"
                if request.language_ambiguous:
                    detected += " (ambiguous)"
                return _escalate(
                    "ESC-12",
                    ev.other(
                        EvidenceKind.LANGUAGE, "detected_language", detected, "language detection"
                    ),
                    ev.counter("language_clarifications", request.counters.language_clarifications),
                )
            return _clarify(ClarifyTarget.LANGUAGE, counts=False)

        # GATE-02: the system must not read or disclose account data before it passes.
        if not state.gate("GATE-02", self._session_valid(request)):
            if request.flags.authentication_declined:
                return _inform(InformReason.AUTHENTICATION_DECLINED)
            if request.counters.authentication_attempts > p.AUTH_MAX_ATTEMPTS:
                return _inform(InformReason.AUTHENTICATION_ATTEMPTS_EXCEEDED)
            return _clarify(ClarifyTarget.AUTHENTICATION, counts=False)
        state.authenticated = True

        # GATE-03
        customer = request.customer
        if customer is None:
            state.note("customer_record_missing")
        customer_active = state.gate(
            "GATE-03", customer is not None and customer.customer_status == "Active"
        )

        # GATE-04 is evaluated per record and does not depend on GATE-03: a record of another
        # customer (or one that does not exist) is refused even when the customer is not
        # active. REFUSE never confirms or denies that the record exists.
        if not state.gate("GATE-04", not request.ownership_violation):
            return _Verdict(Outcome.REFUSE)
        if not customer_active:
            return _escalate(
                "ESC-08",
                ev.record(
                    "customer_status",
                    customer.customer_status if customer else "missing",
                    "customers",
                    customer.customer_id if customer else None,
                ),
            )

        # GATE-05
        reason = request.slots.reason_code
        match = match_transaction(
            request.slots.transaction_ref,
            request.transaction_candidates,
            request.as_of,
            p.LATE_WINDOW_DAYS,
        )
        if match.note is not None:
            state.note(match.note)
        if not state.gate("GATE-05", match.transaction is not None):
            if 2 <= len(match.candidates) <= p.MAX_CANDIDATES_SHOWN:
                ids = tuple(txn.transaction_id for txn in match.candidates)
                return _clarify(ClarifyTarget.TRANSACTION_REF, candidates=ids)
            if match.candidates:
                state.note("too_many_matches")
            target = (
                ClarifyTarget.FEE_REF if reason is ReasonCode.FEE else ClarifyTarget.TRANSACTION_REF
            )
            return _clarify(target)
        txn = match.transaction
        assert txn is not None
        state.transaction = state.disputed = txn
        state.product = next(
            (product for product in request.products if product.product_id == txn.product_id),
            None,
        )

        # GATE-06
        status = txn.transaction_status
        if not state.gate("GATE-06", status == "Approved"):
            if status not in _GATE06_INFORM:
                raise ValueError(f"unexpected transaction_status {status!r}")
            return _inform(_GATE06_INFORM[status])

        # §10: the reason code is asked after the transaction; GATE-07 needs it.
        if reason is None:
            return _clarify(ClarifyTarget.REASON_CODE)

        # GATE-07
        if txn.transaction_type not in DISPUTABILITY:
            raise ValueError(f"unexpected transaction_type {txn.transaction_type!r}")
        mark = DISPUTABILITY[txn.transaction_type][reason]
        if not state.gate("GATE-07", mark is Mark.AUTOMATED):
            if mark is Mark.HUMAN:
                return _escalate(
                    "ESC-14",
                    ev.transaction(txn, "transaction_type", txn.transaction_type),
                    ev.slot("reason_code", reason.value),
                )
            return _inform(InformReason.NOT_DISPUTABLE)

        # GATE-08
        when = txn.transaction_date
        if not state.gate(
            "GATE-08", transaction_within(when, request.as_of, p.DISPUTE_WINDOW_DAYS)
        ):
            if transaction_within(when, request.as_of, p.LATE_WINDOW_DAYS):
                age = transaction_age_days(when, request.as_of)
                return _escalate(
                    "ESC-07",
                    ev.transaction(
                        txn,
                        "transaction_date",
                        f"{when:%Y-%m-%d}, {age} days before the business date "
                        f"(window {p.DISPUTE_WINDOW_DAYS}, late window {p.LATE_WINDOW_DAYS})",
                    ),
                )
            return _inform(InformReason.OUTSIDE_WINDOW)

        # GATE-09
        product = state.product
        if product is None:
            state.note("product_record_missing")
        if not state.gate(
            "GATE-09",
            product is not None and product.product_status in ELIGIBLE_PRODUCT_STATUSES,
        ):
            return _escalate(
                "ESC-08",
                ev.record(
                    "product_status",
                    product.product_status if product else "missing",
                    "products",
                    txn.product_id,
                ),
            )

        # GATE-10
        precondition = self._reason_preconditions(request, state, reason, txn)
        if not state.gate("GATE-10", precondition is None):
            return precondition

        # GATE-11: any case that is not a Draft blocks, open or closed.
        disputed = state.disputed
        existing = [
            case
            for case in request.cases
            if case.transaction_id == disputed.transaction_id
            and case.status is not CaseStatus.DRAFT
        ]
        if not state.gate("GATE-11", not existing):
            latest = max(existing, key=lambda case: (case.business_created_at, case.case_id))
            return _inform(InformReason.DUPLICATE_CASE, existing_case=latest)
        return None

    def _reason_preconditions(
        self,
        request: PolicyRequest,
        state: _State,
        reason: ReasonCode,
        txn: TransactionRecord,
    ) -> _Verdict | None:
        """GATE-10. None when the preconditions of the reason code hold."""
        slots = request.slots
        if reason is ReasonCode.UNRECOGNIZED:
            # A lost or stolen card (card_in_possession = no) passes; ACT-03 is offered.
            if slots.shared_credentials:
                return _escalate("ESC-03", ev.slot("shared_credentials", True))
            if slots.card_in_possession is None:
                return _clarify(ClarifyTarget.CARD_IN_POSSESSION)
            if slots.shared_credentials is None:
                return _clarify(ClarifyTarget.SHARED_CREDENTIALS)
            return None

        if reason is ReasonCode.DUPLICATE:
            return self._duplicate(request, state, txn)

        if reason is ReasonCode.INCORRECT_AMOUNT:
            if slots.expected_amount is None:
                return _clarify(ClarifyTarget.EXPECTED_AMOUNT)
            if slots.expected_amount >= txn.amount:
                return _inform(InformReason.AMOUNT_NOT_EXCEEDED)
            return None

        if reason is ReasonCode.NOT_RECEIVED:
            delivery = slots.expected_delivery_date
            if delivery is None:
                return _clarify(ClarifyTarget.EXPECTED_DELIVERY_DATE)
            if delivery < txn.transaction_date.date():
                # §10 validation: on or after the transaction date.
                state.note("expected_delivery_date_before_transaction")
                return _clarify(ClarifyTarget.EXPECTED_DELIVERY_DATE)
            if delivery >= business_date(request.as_of):
                return _inform(InformReason.DELIVERY_DATE_NOT_REACHED)
            if slots.merchant_contacted is None:
                return _clarify(ClarifyTarget.MERCHANT_CONTACTED)
            if not slots.merchant_contacted:
                return _inform(InformReason.MERCHANT_NOT_CONTACTED)
            return None

        # RC_FEE: the fee is identified when the resolved transaction is an Adjustment, a
        # charge (§17). GATE-07 marks RC_FEE automated only on Adjustment, so it holds here.
        return None

    def _duplicate(
        self, request: PolicyRequest, state: _State, txn: TransactionRecord
    ) -> _Verdict | None:
        """GATE-10 RC_DUPLICATE: find the pair, then have the customer confirm it."""
        slots = request.slots
        twins = duplicate_twins(txn, request.transaction_candidates, self._p.DUPLICATE_WINDOW_HOURS)
        chosen = next((twin for twin in twins if twin.transaction_id == slots.duplicate_ref), None)
        if chosen is not None:
            state.twin, state.disputed = chosen, disputed_of(txn, chosen)
            return None
        if slots.duplicate_ref is not None:
            state.note("duplicate_ref_not_a_twin")
        if not twins:
            state.note("duplicate_not_found")
            return self._ask_other_reason(request)
        twin = nearest_twin(txn, twins)
        state.twin, state.disputed = twin, disputed_of(txn, twin)
        if slots.confirmation is Confirmation.WITHDRAWN:
            return _inform(InformReason.DISPUTE_WITHDRAWN)
        if slots.confirmation is Confirmation.DECLINED:
            state.note("duplicate_declined")
            return self._ask_other_reason(request)
        return _clarify(ClarifyTarget.DUPLICATE_REF)

    @staticmethod
    def _ask_other_reason(request: PolicyRequest) -> _Verdict:
        """RC_DUPLICATE not established: ask exactly once whether the customer means another
        reason (``duplicate_reason_reasked``); if it remains unresolved, ESC-09. The re-ask is
        an ordinary clarification, so the §10 limits still apply to it."""
        if request.counters.duplicate_reason_reasked:
            return _escalate(
                "ESC-09",
                ev.counter("duplicate_reason_reasked", "true"),
                ev.slot("reason_code", ReasonCode.DUPLICATE.value),
            )
        return _Verdict(Outcome.CLARIFY, clarify_target=ClarifyTarget.REASON_CODE, reask=True)

    # ------------------------------------------------------------------ channel 3

    def _record_triggers(
        self, request: PolicyRequest, state: _State, disputed: TransactionRecord, tier: Tier
    ) -> None:
        p = self._p
        if tier is Tier.T3:
            amount = (
                f"{disputed.amount_usd} (above {p.AUTO_INTAKE_MAX_USD})"
                if disputed.amount_usd is not None
                else "unknown (treated as T3)"
            )
            state.fire("ESC-01", [ev.transaction(disputed, "amount_usd", amount)])

        counted = [case for case in request.cases if case.status is not CaseStatus.DRAFT]

        def recent(case: CaseRecord, days: int) -> bool:
            return within_window(case.business_created_at, request.as_of, timedelta(days=days))

        disputed_30d = sum(
            (case.amount_usd for case in counted if recent(case, VELOCITY_AMOUNT_DAYS)),
            disputed.amount_usd or Decimal(0),
        )
        cases_90d = sum(1 for case in counted if recent(case, VELOCITY_COUNT_DAYS))
        velocity: list[Evidence] = []
        if disputed_30d > p.AGG_DISPUTED_30D_MAX_USD:
            velocity.append(
                ev.record(
                    "disputed_usd_30d",
                    f"{disputed_30d} including this dispute (limit {p.AGG_DISPUTED_30D_MAX_USD})",
                    "cases",
                    None,
                    ev.CASES,
                )
            )
        if cases_90d >= p.REPEAT_DISPUTES_90D:
            velocity.append(
                ev.record(
                    "cases_90d",
                    f"{cases_90d} previous cases (limit {p.REPEAT_DISPUTES_90D})",
                    "cases",
                    None,
                    ev.CASES,
                )
            )
        if velocity:
            state.fire("ESC-02", velocity)

        if disputed.fraud_score is None:
            state.note("fraud_score_missing")
        elif disputed.fraud_score >= p.FRAUD_SCORE_ESCALATE:
            state.fire(
                "ESC-04",
                [
                    ev.transaction(
                        disputed,
                        "fraud_score",
                        f"{disputed.fraud_score} (threshold {p.FRAUD_SCORE_ESCALATE})",
                    )
                ],
            )

    # ------------------------------------------------------------------ confirmation

    @staticmethod
    def _confirmation(request: PolicyRequest) -> _Verdict:
        """COM-03 for ACT-02: the reply to the summary (policy §8)."""
        reply = request.slots.confirmation
        if reply is Confirmation.CONFIRMED:
            return _Verdict(Outcome.RESOLVE)
        if reply is Confirmation.WITHDRAWN:
            return _inform(InformReason.DISPUTE_WITHDRAWN)
        if reply is Confirmation.DECLINED:
            return _clarify(ClarifyTarget.CORRECTION)
        # None presents the summary (not a clarification); a hedged reply asks once more.
        return _clarify(ClarifyTarget.CONFIRMATION, counts=reply is Confirmation.HEDGED)

    # ------------------------------------------------------------------ helpers

    def _session_valid(self, request: PolicyRequest) -> bool:
        session = request.session
        if session is None:
            return False
        age = request.now - session.issued_at
        idle = request.now - session.last_activity_at
        return age < timedelta(minutes=self._p.SESSION_MAX_AGE_MIN) and idle < timedelta(
            minutes=self._p.SESSION_IDLE_TIMEOUT_MIN
        )

    def _tier(self, amount_usd: Decimal | None) -> Tier:
        """§6, with ``>`` and ``<=`` as written. An unknown amount is treated as T3."""
        if amount_usd is None or amount_usd > self._p.AUTO_INTAKE_MAX_USD:
            return Tier.T3
        if amount_usd > self._p.PROVISIONAL_CREDIT_AUTO_MAX_USD:
            return Tier.T2
        return Tier.T1

    def _clarifications_exhausted(
        self, request: PolicyRequest, verdict: _Verdict
    ) -> list[Evidence]:
        # Only verdicts that count reach here: language and authentication have their own limits.
        target = verdict.clarify_target
        assert target is not None
        counters, p = request.counters, self._p
        found: list[Evidence] = []
        asked = counters.clarifications_by_slot.get(target, 0)
        if asked >= p.MAX_CLARIFICATION_TURNS:
            found.append(
                ev.counter(
                    f"clarifications_by_slot.{target}",
                    f"{asked} (limit {p.MAX_CLARIFICATION_TURNS})",
                )
            )
        if counters.total_clarifications >= p.MAX_TOTAL_CLARIFICATIONS:
            found.append(
                ev.counter(
                    "total_clarifications",
                    f"{counters.total_clarifications} (limit {p.MAX_TOTAL_CLARIFICATIONS})",
                )
            )
        return found

    def _model_uncertain(self, request: PolicyRequest) -> list[Evidence]:
        """ESC-11. Unavailable signals are unknown uncertainty and always fire. The thresholds
        apply only to Kev's signals; the extraction fallback (0/1, uncalibrated) never fires."""
        signals = request.signals
        origin = f"decision layer ({signals.source})"
        if signals.source is ModelSource.UNAVAILABLE:
            return [ev.other(EvidenceKind.SIGNAL, "source", "unavailable", origin)]
        if signals.source is not ModelSource.KEV:
            return []
        found: list[Evidence] = []
        confidence_min = self._p.DECISION_CONFIDENCE_MIN
        if confidence_min is not None:
            top = signals.top_reason_code
            if top is None or top[1] < confidence_min:
                value = f"{top[0]} {top[1]}" if top else "none"
                found.append(
                    ev.other(
                        EvidenceKind.SIGNAL,
                        "top_reason_code",
                        f"{value} (minimum {confidence_min})",
                        origin,
                    )
                )
        risk_threshold = self._p.ESCALATION_RISK_THRESHOLD
        if (
            risk_threshold is not None
            and signals.escalation_risk is not None
            and signals.escalation_risk >= risk_threshold
        ):
            found.append(
                ev.other(
                    EvidenceKind.SIGNAL,
                    "escalation_risk",
                    f"{signals.escalation_risk} (threshold {risk_threshold})",
                    origin,
                )
            )
        return found

    @staticmethod
    def _esc14_queue(request: PolicyRequest, state: _State) -> Queue:
        txn = state.transaction
        if (
            request.slots.reason_code is ReasonCode.UNRECOGNIZED
            and txn is not None
            and txn.transaction_type in ACCOUNT_INITIATED_TYPES
        ):
            return Queue.FRAUD
        return Queue.DISPUTES

    def _card_block(
        self, request: PolicyRequest, state: _State
    ) -> tuple[ProductRecord | None, bool]:
        """§8 card block offer: the card to block, and whether it is already blocked."""
        unrecognized = request.slots.reason_code is ReasonCode.UNRECOGNIZED
        takeover = "ESC-03" in state.fired
        if not state.authenticated or not (unrecognized or takeover):
            return None, False
        if state.transaction is not None:
            product = state.product
            if product is None or product.product_type not in CARD_PRODUCT_TYPES:
                state.note("no_card_to_block")
                return None, False
            if product.product_status == "Active":
                return product, False
            return None, product.product_status == "Blocked"
        if not takeover:
            return None, False
        active_cards = [
            product
            for product in request.products
            if product.product_type in CARD_PRODUCT_TYPES and product.product_status == "Active"
        ]
        if len(active_cards) == 1:
            return active_cards[0], False
        state.note("card_block_not_offered_several_cards" if active_cards else "no_active_card")
        return None, False

    # ------------------------------------------------------------------ decision

    def _decision(
        self,
        request: PolicyRequest,
        state: _State,
        verdict: _Verdict | None,
        tier: Tier | None,
        amount_usd: Decimal | None,
    ) -> PolicyDecision:
        if verdict is not None and verdict.outcome is Outcome.REFUSE:
            outcome = Outcome.REFUSE
        elif state.fired:
            outcome = Outcome.ESCALATE
        else:
            assert verdict is not None
            outcome = verdict.outcome

        actions: list[ActionId] = []
        card: ProductRecord | None = None
        already_blocked = False
        if outcome is not Outcome.REFUSE:
            card, already_blocked = self._card_block(request, state)
            if card is not None:
                actions.append(ActionId.BLOCK_CARD)
        if outcome is Outcome.RESOLVE:
            actions += [ActionId.CREATE_CASE, ActionId.RECORD_CREDIT_FLAG]
        if outcome is Outcome.ESCALATE:
            actions.append(ActionId.TRANSFER_TO_HUMAN)

        queue = priority = None
        if outcome is Outcome.ESCALATE:
            routes = state.fired.values()
            queue = max((route[0] for route in routes), key=QUEUE_RANK.__getitem__)
            high = any(route[1] is Priority.HIGH for route in routes)
            priority = Priority.HIGH if high else Priority.NORMAL

        flag = (
            {
                Tier.T1: ProvisionalCreditFlag.ELIGIBLE,
                Tier.T2: ProvisionalCreditFlag.REQUIRES_REVIEW,
            }.get(tier)
            if tier is not None
            else None
        )
        existing = verdict.existing_case if verdict is not None else None
        clarify = outcome is Outcome.CLARIFY and verdict is not None
        return PolicyDecision(
            outcome=outcome,
            policy_version=self.policy_version,
            gates_evaluated=state.gates,
            triggered_rules=sorted(state.fired),
            authorized_actions=actions,
            queue=queue,
            priority=priority,
            tier=tier,
            amount_usd=amount_usd,
            provisional_credit_flag=flag,
            clarify_target=verdict.clarify_target if clarify and verdict else None,
            inform_reason=(
                verdict.inform_reason if outcome is Outcome.INFORM and verdict else None
            ),
            transaction_id=state.disputed.transaction_id if state.disputed else None,
            reason_code=request.slots.reason_code if state.authenticated else None,
            candidate_transaction_ids=list(verdict.candidates) if clarify and verdict else [],
            duplicate_transaction_id=state.twin.transaction_id if state.twin else None,
            existing_case_id=existing.case_id if existing and outcome is Outcome.INFORM else None,
            existing_case_status=(
                existing.status if existing and outcome is Outcome.INFORM else None
            ),
            card_product_id=card.product_id if card else None,
            card_already_blocked=already_blocked,
            duplicate_reason_reask=bool(clarify and verdict and verdict.reask),
            notes=state.notes,
            evidence=[
                RuleEvidence(rule_id=rule, evidence=state.evidence[rule])
                for rule in sorted(state.fired)
            ],
        )

    # ------------------------------------------------------------------ explain

    def explain(self, decision: PolicyDecision) -> list[str]:
        """Explanation from rule identifiers and records (policy principle 5)."""
        lines = [f"Outcome {decision.outcome} under policy {decision.policy_version}."]
        for gate in decision.gates_evaluated:
            result = "passed" if gate.passed else "did not pass"
            lines.append(f"{gate.gate_id} ({GATE_NAMES[gate.gate_id]}) {result}.")
        if decision.transaction_id is not None:
            reason = decision.reason_code or "no reason code yet"
            lines.append(f"Transaction {decision.transaction_id}, {reason}.")
        if decision.tier is not None:
            amount = (
                f"USD {decision.amount_usd}"
                if decision.amount_usd is not None
                else "unknown USD amount"
            )
            lines.append(f"Tier {decision.tier} ({amount}).")
        for rule in decision.triggered_rules:
            lines.append(f"{rule} ({RULE_NAMES[rule]}) fired.")
        if decision.queue is not None:
            lines.append(f"Routed to the {decision.queue} queue, {decision.priority} priority.")
        if decision.inform_reason is not None:
            lines.append(f"Inform: {decision.inform_reason}.")
        if decision.existing_case_id is not None:
            lines.append(
                f"Existing case {decision.existing_case_id} ({decision.existing_case_status})."
            )
        if decision.clarify_target is not None:
            lines.append(f"Clarify: {decision.clarify_target}.")
        if decision.candidate_transaction_ids:
            lines.append(f"Candidates: {', '.join(decision.candidate_transaction_ids)}.")
        if decision.duplicate_transaction_id is not None:
            lines.append(f"Duplicate pair with {decision.duplicate_transaction_id}.")
        for action in decision.authorized_actions:
            lines.append(f"{action} ({ACTION_NAMES[action]}) authorized.")
        if decision.card_product_id is not None:
            lines.append(f"Card {decision.card_product_id} may be blocked.")
        if decision.card_already_blocked:
            lines.append("The card is already blocked.")
        for note in decision.notes:
            lines.append(f"Note: {note}.")
        return lines
