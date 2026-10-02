"""M12 Orchestrator: one customer message per turn, through the modules of architecture §4.

Session -> Input Guard -> LLM Adapter and Decision Client in parallel -> Policy Engine with the
Tool Layer's records -> action and read-back -> Handoff Builder on ESCALATE -> templates (and
connecting sentences) -> one audit trace -> reply.

Contracts it keeps (each has a test):

1. One turn deadline (``LLM_TURN_DEADLINE_SECONDS``): Kev runs in parallel with ``extract``;
   ``connect`` gets only what is left.
2. A message flagged by the Input Guard reaches neither the LLM nor Kev; the reply is
   ``ask_rephrase``; it is not a clarification; strikes count per ``conversation_id``. At the
   strike limit the turn escalates (ESC-13) without the models.
3. If ``extract`` fails, the turn goes on with empty slots and the rule-based signals; no
   exception reaches the customer. Past ``LLM_MAX_TOKENS_PER_CONVERSATION`` every turn behaves
   the same way, without calling the LLM.
4. Any unexpected exception (engine, Tool Layer, templates) becomes a handoff with the
   ``tool_failure`` notice and a trace with the error, never an HTTP error with a stack trace.
5. One confirmation per action (COM-03); the card block before the dispute summary; declining
   the block does not affect the dispute. ``confirmation`` is cleared after it is used and when
   a slot changes after the summary; the first summary is not a clarification.
6. A declined summary is corrected with ``app.orchestrator.corrections``.
7. ESC-05 is honored at once; a pending or unoffered card block goes to the open questions.
8. ESC-03: the card block confirmation turn comes first, then the handoff; that turn is not a
   clarification.
9. Replies follow the template composition rules (``app.orchestrator.replies``).
10. ``detected_language`` is the conversation's language; amounts use language + country.
11. A session that expired with a confirmation pending gets ``session_expired_reconfirm``, and
    that confirmation no longer counts.
12. Exactly one trace per turn.
13. The builder receives ``slot_turns``, ``transaction_ref_said`` and ``picked_candidate``.
14. A side question (``SideQuestion``) is answered with a template before the rest of the
    reply. It is neither a clarification nor an unclear answer: a side question alone repeats
    the pending question without counting it (the card block offer too, without spending
    ``BLOCK_REASKS``). ``other`` with nothing pending and nothing else said gets
    ``side_unsupported`` + ``offer_transfer`` and does not push into the dispute flow.
15. The block offer shows the charge it is about. "Ese no es" (``wrong_transaction``) corrects
    the transaction: GATE-05 runs again and nothing is blocked. An offer without a clear
    answer is never dropped in silence (``block_not_done``) and stays available until the case
    is created: a later ``block_card_requested`` offers it again, with its confirmation.
16. Every trace records an outcome and a ``reply_kind``, also when the engine is not called
    (block offer and ``ask_rephrase``: CLARIFY; side question ``other``: INFORM).
17. Without a session Kev is not called (no rule before GATE-02 uses its signals), and
    ``connect`` is not called when the reply asks to log in. ``extract`` still runs (language,
    interrupts), and its slots are kept for after the login.
18. GATE-05 results are told (policy 0.4.7): candidates from a relaxed search are listed even
    alone (one is confirmed with yes/no), ``no_match`` says what was searched and asks for a
    missing detail, too many matches ask for the most useful one. The same clarification text
    is never sent two turns in a row (``VARIANT_TEMPLATES``).
19. A clarification answered with new information is not counted toward ESC-09.
20. ``flow_help`` is answered with how to go on, only when the message brings no detail (a
    detail is processed instead); ``offer_transfer`` after ``other`` once per conversation.
21. ``connect`` writes full sentences only on the first turn, bad news (INFORM, ESCALATE,
    REFUSE, ``no_match``); otherwise a brief acknowledgment at most. It receives the sentences
    already sent, so none is repeated. A turn that answers a side question ``other`` gets no
    connecting sentence: nothing may sound like accepting the request.
22. ``card_already_blocked`` is only for a card blocked before the conversation, never for one
    ACT-03 blocked in it. After RESOLVE or INFORM, a message with no detail, no signal and no
    side question ("gracias", "ok", "obrigado") gets ``closing``, not a new question; the turn
    keeps the final outcome, so the metrics still see the conversation's result.
23. The handoff text is sent once, in the turn that escalates. Every later message gets
    ``already_transferred`` (neutral: no reason, no hint of detection), reaches no model and
    no Input Guard, and is added masked to the packet (``post_handoff_messages``) for the
    agent. When it could not be added, ``already_transferred_short`` says nothing about the
    agent seeing it (COM-04).
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, TypeVar

from app.audit.cost import TokenRates, estimate_cost
from app.audit.masking import mask_message
from app.config import PolicyParameters, load_merchant_categories
from app.contracts import (
    AccessDeniedError,
    ActionId,
    CaseRecord,
    CaseStatus,
    ClarifyTarget,
    Confirmation,
    ConversationFlags,
    CustomerRecord,
    Evidence,
    EvidenceKind,
    ExtractionResult,
    InputGuardResult,
    Language,
    LLMContext,
    LLMTransaction,
    ModelSignals,
    ModelSource,
    Outcome,
    PolicyDecision,
    PolicyRequest,
    Priority,
    ProductRecord,
    Queue,
    ReasonCode,
    RuleEvidence,
    SessionContext,
    SideQuestion,
    SlotName,
    Slots,
    ToolResult,
    ToolStatus,
    TraceRecord,
    TransactionField,
    TransactionRecord,
    TransactionRef,
    TransactionSearch,
)
from app.deadline import Deadline
from app.decision.fallback import resolve_signals
from app.interfaces import (
    AuditTracer,
    DecisionClient,
    HandoffBuilder,
    IdentityService,
    InputGuard,
    LLMAdapter,
    PolicyEngine,
    ToolLayer,
)
from app.llm_adapter.adapter import ExtractionUnavailableError
from app.llm_adapter.language import guess_language
from app.llm_adapter.signals import RuleBasedSignalDetector
from app.orchestrator.calls import collect_calls
from app.orchestrator.corrections import apply_declined_correction
from app.orchestrator.replies import HANDOFF_IDS, Reply
from app.orchestrator.state import (
    BlockOffer,
    ConversationState,
    ConversationStore,
    Pending,
)
from app.policy.clock import business_date
from app.policy.matching import given_details
from app.pseudonym import UNAUTHENTICATED_REF, customer_ref
from app.templates.formatting import (
    Locale,
    currency_said,
    format_amount,
    format_date,
    locale_for,
    plain_number,
)
from app.templates.service import (
    INFORM_TEMPLATES,
    SIDE_TEMPLATES,
    VARIANT_TEMPLATES,
    TemplateService,
)

T = TypeVar("T")
Clock = Callable[[], datetime]
ToolFactory = Callable[[SessionContext | None, str], ToolLayer]

SUPPORTED = frozenset(language.value for language in Language)
NOT_COUNTED = frozenset({ClarifyTarget.LANGUAGE, ClarifyTarget.AUTHENTICATION})
SUMMARY_TARGETS = frozenset({ClarifyTarget.CONFIRMATION, ClarifyTarget.CORRECTION})
BLOCK_REASKS = 1  # an unclear answer to the block offer is asked once more (policy §8)
MIN_GUESS_WORDS = 3  # the rule-based language guess needs a few words
LOGIN_IDS = frozenset({"ask_authentication", "session_expired_reconfirm"})
CASE_REF = re.compile(r"\bDSP-\d{8}-\d{6,}\b", re.IGNORECASE)
MAX_CASES_SHOWN = 3  # side question case_status: the most recent cases
MAX_PREVIOUS_SENTENCES = 6  # connecting sentences passed back to connect (contract 21)
TRANSACTION_ASKS = frozenset(
    {
        "clarify_transaction_ref",
        "clarify_transaction_ref_again",
        "clarify_transaction_ref_known",
        "ask_transaction_detail",
    }
)
SEARCH_ASKS = frozenset({"clarify_transaction_ref", "no_match", "ask_many_detail"})
BAD_NEWS = frozenset({Outcome.INFORM, Outcome.ESCALATE, Outcome.REFUSE})
Prefix = list[tuple[str, dict[str, object]]]


class ConversationAccessError(Exception):
    """The conversation belongs to another customer."""


@dataclass(frozen=True)
class OrchestratorConfig:
    as_of: datetime
    parameters: PolicyParameters
    policy_version: str
    pseudonym_key: str
    turn_deadline_seconds: float
    token_cap: int | None = None  # LLM tokens per conversation (architecture §7)
    rates: TokenRates | None = None


@dataclass(frozen=True)
class TurnResult:
    conversation_id: str
    trace_id: str
    turn_index: int
    reply: str
    reply_kind: str
    outcome: Outcome | None
    language: Language | None
    closed: bool


@dataclass
class _Records:
    customer: CustomerRecord | None = None
    products: list[ProductRecord] = field(default_factory=list)
    pool: list[TransactionRecord] = field(default_factory=list)
    cases: list[Any] = field(default_factory=list)
    country: str | None = None


@dataclass
class _Turn:
    state: ConversationState
    message: str
    trace_id: str
    created_at: datetime
    session: SessionContext | None = None
    tools: ToolLayer | None = None
    guard: InputGuardResult | None = None
    records: _Records = field(default_factory=_Records)
    context: LLMContext | None = None
    deadline: Deadline | None = None
    extraction: ExtractionResult | None = None
    llm_ok: bool = True
    signals: ModelSignals | None = None
    detected_language: str | None = None
    language_ambiguous: bool = False
    decisions: list[PolicyDecision] = field(default_factory=list)
    tool_calls: list[ToolResult] = field(default_factory=list)
    stages: dict[str, float] = field(default_factory=dict)
    prefix: Prefix = field(default_factory=list)
    side_question: SideQuestion | None = None
    side_only: bool = False  # a side question and no answer to anything (contract 14)
    asked_before: ClarifyTarget | None = None  # the CLARIFY target this message answers
    clarify_key: tuple[ClarifyTarget, str] | None = None  # the clarification sent (contract 18)
    # The pending clarification counted and was not answered: a side question that repeats it
    # keeps it so, and the next answer with new information takes it back (contract 19).
    still_counted: bool = False
    reply: Reply | None = None
    reply_kind: str = ""
    outcome: Outcome | None = None
    handoff_id: str | None = None
    error: str | None = None


class Orchestrator:
    def __init__(
        self,
        *,
        identity: IdentityService | None,
        guard: InputGuard,
        llm: LLMAdapter,
        decision: DecisionClient,
        engine: PolicyEngine,
        tools: ToolFactory,
        builder: HandoffBuilder,
        templates: TemplateService,
        tracer: AuditTracer,
        store: ConversationStore,
        config: OrchestratorConfig,
        clock: Clock | None = None,
    ) -> None:
        self._identity = identity
        self._guard = guard
        self._llm = llm
        self._decision = decision
        self._engine = engine
        self._tools = tools
        self._builder = builder
        self._templates = templates
        self._tracer = tracer
        self._store = store
        self._config = config
        self._p = config.parameters
        self._clock = clock or (lambda: datetime.now(UTC))
        self._detector = RuleBasedSignalDetector()
        self._locks: dict[str, asyncio.Lock] = {}
        self._categories = load_merchant_categories()  # the same words GATE-05 reads

    # ------------------------------------------------------------------ public API

    async def handle_turn(
        self, conversation_id: str | None, message: str, token: str | None = None
    ) -> TurnResult:
        cid = conversation_id or f"CONV-{uuid.uuid4().hex[:16]}"
        lock = self._locks.setdefault(cid, asyncio.Lock())
        async with lock:
            state = self._store.get(cid) or ConversationState(conversation_id=cid)
            turn = _Turn(
                state=state,
                message=message,
                trace_id=f"TRC-{uuid.uuid4().hex}",
                created_at=self._clock(),
            )
            started = time.perf_counter()
            with collect_calls() as calls:
                try:
                    await self._run_turn(turn, token)
                except ConversationAccessError:
                    raise
                except Exception as exc:  # contract 4: never an error page with a trace
                    await self._emergency(turn, exc)
                text = await self._finish_reply(turn)
            total_ms = (time.perf_counter() - started) * 1000
            self._trace(turn, calls, total_ms)
            state.turn_index += 1
            self._store.save(state)
            return TurnResult(
                conversation_id=cid,
                trace_id=turn.trace_id,
                turn_index=state.turn_index - 1,
                reply=text,
                reply_kind=turn.reply_kind,
                outcome=turn.outcome,
                language=state.language,
                closed=state.closed,
            )

    # ------------------------------------------------------------------ the turn

    async def _run_turn(self, turn: _Turn, token: str | None) -> None:
        state = turn.state
        with self._stage(turn, "identity"):
            session = (
                await self._io(self._identity.validate_session, token)
                if token and self._identity is not None
                else None
            )
        if session is not None:
            if state.customer_id is not None and session.customer_id != state.customer_id:
                raise ConversationAccessError(state.conversation_id)
            state.customer_id = session.customer_id
        turn.session = session
        turn.tools = self._tools(session, state.conversation_id)

        if state.closed:
            # Contract 23: said once; now only a neutral notice, and the message for the agent.
            kept = await self._keep_for_agent(turn)
            notice = "already_transferred" if kept else "already_transferred_short"
            self._new_reply(turn).add(notice)
            turn.reply_kind = "closed"
            turn.outcome = Outcome.ESCALATE
            turn.handoff_id = state.handoff_id
            return

        with self._stage(turn, "input_guard"):
            guard = await self._io(
                self._guard.inspect, state.conversation_id, turn.message, session
            )
        turn.guard = guard
        state.counters = state.counters.model_copy(
            update={"injection_strikes": max(state.counters.injection_strikes, guard.strikes)}
        )
        if guard.flagged and not guard.escalate_security:
            # Contract 2: nothing reaches the models; not a clarification.
            self._new_reply(turn).add("ask_rephrase")
            turn.reply_kind, turn.outcome = "ask_rephrase", Outcome.CLARIFY
            return

        if session is not None:
            with self._stage(turn, "records"):
                await self._read_records(turn)
        turn.context = self._context(turn)

        if guard.flagged:  # ESC-13 at the strike limit: the message reaches no model
            turn.signals = ModelSignals(source=ModelSource.UNAVAILABLE)
        else:
            with self._stage(turn, "models"):
                await self._understand(turn)
        self._resolve_language(turn)
        if await self._merge(turn):
            return
        await self._evaluate_and_act(turn)

    async def _keep_for_agent(self, turn: _Turn) -> bool:
        """Add the message, masked, to the handoff packet; True when it was added. Without the
        session that owns the handoff (it expired) nothing is written."""
        state = turn.state
        if state.handoff_id is None or turn.tools is None:
            turn.error = "post-handoff message not added: no handoff"
            return False
        if state.customer_id is not None and turn.session is None:
            turn.error = "post-handoff message not added: no session"
            return False
        try:
            with self._stage(turn, "tools"):
                await self._io(
                    turn.tools.append_handoff_message, state.handoff_id, mask_message(turn.message)
                )
        except Exception as exc:  # the notice still goes out, without promising the agent sees it
            turn.error = f"post-handoff message not added: {type(exc).__name__}"
            return False
        return True

    async def _read_records(self, turn: _Turn) -> None:
        tools = turn.tools
        assert tools is not None
        records = turn.records
        records.customer = await self._io(tools.get_customer)
        records.products = await self._io(tools.list_products)
        records.pool = await self._io(tools.transaction_candidates)
        records.cases = await self._io(tools.list_cases)
        records.country = await self._io(tools.customer_country)

    def _context(self, turn: _Turn) -> LLMContext:
        state, session = turn.state, turn.session
        return LLMContext(
            customer_ref=(
                customer_ref(session.customer_id, self._config.pseudonym_key)
                if session
                else UNAUTHENTICATED_REF
            ),
            language=state.language,
            masked_products=[p.product_number_masked for p in turn.records.products],
            transactions=[
                LLMTransaction(
                    transaction_ref=txn.transaction_id,
                    transaction_date=txn.transaction_date.date(),
                    amount=txn.amount,
                    currency=txn.currency,
                    merchant_name=txn.merchant_name,
                    transaction_status=txn.transaction_status,
                )
                for txn in turn.records.pool
            ],
            pending_slot=self._pending_slot(state),
            shown_candidates=list(state.shown_candidates),
            business_date=business_date(self._config.as_of),
        )

    @staticmethod
    def _pending_slot(state: ConversationState) -> SlotName | None:
        if state.pending in (Pending.BLOCK_OFFER, Pending.SUMMARY):
            return SlotName.CONFIRMATION
        target = state.pending_target
        if state.pending is not Pending.CLARIFY or target is None:
            return None
        if target is ClarifyTarget.CORRECTION:
            return SlotName.CONFIRMATION  # "mejor no" withdraws, details correct
        if target is ClarifyTarget.TRANSACTION_REF and len(state.shown_candidates) == 1:
            return SlotName.CONFIRMATION  # "¿Es esta?" (contract 18)
        if target in NOT_COUNTED:
            return None
        return SlotName(target.value)

    async def _understand(self, turn: _Turn) -> None:
        """Contract 1: extract and Kev in parallel on one deadline."""
        state, context = turn.state, turn.context
        assert context is not None
        cap = self._config.token_cap
        if cap is not None and state.tokens_used >= cap:
            # Token cap reached (architecture §7): the LLM is not called again.
            turn.llm_ok = False
            turn.extraction = ExtractionResult(flags=self._detector.detect(turn.message))
            kev = await self._kev(turn, None)
        else:
            turn.deadline = Deadline(self._config.turn_deadline_seconds)
            turn.extraction, kev = await asyncio.gather(
                self._extract(turn, turn.deadline), self._kev(turn, turn.deadline)
            )
        turn.signals = resolve_signals(kev, turn.extraction)

    async def _extract(self, turn: _Turn, deadline: Deadline) -> ExtractionResult:
        assert turn.context is not None
        try:
            return await self._llm.extract(turn.message, turn.context, deadline)
        except ExtractionUnavailableError as exc:  # contract 3
            turn.llm_ok = False
            return exc.fallback
        except Exception:
            turn.llm_ok = False
            return ExtractionResult(flags=self._detector.detect(turn.message))

    async def _kev(self, turn: _Turn, deadline: Deadline | None) -> ModelSignals:
        assert turn.context is not None
        if turn.session is None:  # contract 17
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        try:
            return await self._decision.signals(turn.message, turn.context, deadline)
        except Exception:
            return ModelSignals(source=ModelSource.UNAVAILABLE)

    def _resolve_language(self, turn: _Turn) -> None:
        """Contract 10: the conversation keeps its language until a message is clearly in
        another one; short or ambiguous messages ("C2", "sí") do not change it."""
        state, extraction = turn.state, turn.extraction
        detected = extraction.detected_language if extraction else None
        ambiguous = extraction.language_ambiguous if extraction else True
        if detected is None and not turn.llm_ok:
            guessed = guess_language(turn.message)
            detected, ambiguous = (guessed.value, False) if guessed else (None, True)
        if detected in SUPPORTED and not ambiguous:
            state.language = Language(detected)
            turn.detected_language, turn.language_ambiguous = detected, False
        elif detected is not None and not ambiguous:
            turn.detected_language, turn.language_ambiguous = detected, False
        elif state.language is not None:
            turn.detected_language, turn.language_ambiguous = state.language.value, False
        elif (
            len(turn.message.split()) >= MIN_GUESS_WORDS
            and (guessed := guess_language(turn.message)) is not None
        ):
            # A first message the model found ambiguous: the rule-based guess decides before
            # the customer is asked which language they prefer (not for one or two words,
            # where it is unreliable: "no sé" reads as Portuguese).
            state.language = guessed
            turn.detected_language, turn.language_ambiguous = guessed.value, False
        else:
            turn.detected_language, turn.language_ambiguous = detected, True

    # ------------------------------------------------------------------ merging the message

    async def _merge(self, turn: _Turn) -> bool:
        """Apply the extraction to the conversation. True when the reply is already decided."""
        state, extraction = turn.state, turn.extraction
        if extraction is None:
            return False
        claims = [c for c in extraction.customer_claims if c not in state.claims]
        state.claims.extend(claims)
        self._merge_flags(state, extraction.flags, extraction.customer_claims)
        new = self._with_currency(turn, extraction.slots)
        answer = new.confirmation
        pending, target = state.pending, state.pending_target
        state.pending, state.pending_target = Pending.NONE, None
        urgent = state.flags.human_requested or state.flags.legal_or_vulnerability
        if pending is Pending.CLARIFY and state.pending_counted:
            if self._informative(state, new):
                assert target is not None
                self._uncount(state, target)  # contract 19: answered with new information
            else:
                turn.still_counted = True
        state.pending_counted = False
        question = extraction.side_question
        answered = new.model_copy(update={"confirmation": None}) != Slots() or answer not in (
            None,
            Confirmation.HEDGED,
        )
        turn.side_only = (
            question is not None
            and not answered
            and not extraction.wrong_transaction
            and not extraction.block_card_requested
        )
        turn.asked_before = target if pending is Pending.CLARIFY else None
        if (
            state.last_outcome in (Outcome.RESOLVE, Outcome.INFORM)
            and pending is Pending.NONE
            and not answered
            and question is None
            and extraction.flags == ConversationFlags()
            and not extraction.wrong_transaction
            and not extraction.block_card_requested
        ):
            # Contract 22: "gracias" after a final outcome closes; it asks for nothing.
            self._new_reply(turn).add("closing")
            turn.reply_kind, turn.outcome = "closing", state.last_outcome
            return True
        if question is SideQuestion.FLOW_HELP and answered:
            question = None  # contract 20: the detail is processed, no help text
            turn.side_only = False
        if question is not None:
            await self._answer_side_question(turn, question)
            if (
                question is SideQuestion.OTHER
                and pending is Pending.NONE
                and turn.side_only
                and extraction.flags == ConversationFlags()
            ):
                # Contract 14: nothing pending, nothing else said: no push into the dispute flow.
                self._new_reply(turn)
                turn.reply_kind, turn.outcome = "side:other", Outcome.INFORM
                return True

        if (
            extraction.block_card_requested
            and pending is not Pending.BLOCK_OFFER
            and state.block_offer is BlockOffer.DECLINED
            and state.block_product_id is not None
            and not urgent
        ):
            # Contract 15: the offer stands until the case is created; asked again, confirmed.
            self._apply_slots(
                turn, new.model_copy(update={"confirmation": None}), extraction.customer_claims
            )
            state.block_offer, state.block_reasks = BlockOffer.OFFERED, 0
            state.pending = Pending.BLOCK_OFFER
            self._offer_block(turn)
            return True

        if pending is Pending.BLOCK_OFFER:
            if extraction.wrong_transaction:
                self._reject_identified(turn, new, extraction.customer_claims)
                return False
            # Whatever else the customer said is kept, even when the block is asked again.
            self._apply_slots(
                turn, new.model_copy(update={"confirmation": None}), extraction.customer_claims
            )
            if answer is Confirmation.CONFIRMED:
                await self._block_card(turn)
            elif answer in (Confirmation.DECLINED, Confirmation.WITHDRAWN):
                state.block_offer = BlockOffer.DECLINED  # the dispute goes on (COM-03)
            elif urgent:
                pass  # still offered and unconfirmed: the handoff tells the agent (contract 7)
            elif turn.side_only:
                state.pending = Pending.BLOCK_OFFER  # contract 14: no BLOCK_REASKS spent
                self._offer_block(turn)
                return True
            elif state.block_reasks < BLOCK_REASKS:
                state.block_reasks += 1
                self._count(state, ClarifyTarget.CONFIRMATION)
                state.pending = Pending.BLOCK_OFFER
                self._offer_block(turn)
                return True
            else:
                # No clear yes: nothing is blocked, and the customer is told (contract 15).
                state.block_offer = BlockOffer.DECLINED
                turn.prefix.append(("block_not_done", {}))
            return False

        summary_reply = pending is Pending.SUMMARY or (
            pending is Pending.CLARIFY and target in SUMMARY_TARGETS
        )
        if summary_reply:
            correcting = answer is Confirmation.DECLINED or (
                target is ClarifyTarget.CORRECTION and answer is not Confirmation.WITHDRAWN
            )
            if correcting:
                return self._correct(turn, new, extraction.customer_claims)
            changed = self._apply_slots(turn, new, extraction.customer_claims)
            if not changed and answer is not None:
                state.slots = state.slots.model_copy(update={"confirmation": answer})
            return False

        if pending is Pending.CLARIFY and target is ClarifyTarget.DUPLICATE_REF:
            if answer is Confirmation.CONFIRMED and state.pending_duplicate_id:
                state.slots = state.slots.model_copy(
                    update={"duplicate_ref": state.pending_duplicate_id, "confirmation": None}
                )
                self._mark(turn, SlotName.DUPLICATE_REF, extraction.customer_claims)
                return False
            if answer is not None:
                state.slots = state.slots.model_copy(update={"confirmation": answer})
                return False

        if (
            pending is Pending.CLARIFY
            and target is ClarifyTarget.TRANSACTION_REF
            and len(state.shown_candidates) == 1
            and new.transaction_ref is None
        ):
            # "¿Es esta?" about one candidate of a relaxed search (contract 18).
            if answer is Confirmation.CONFIRMED:
                picked = TransactionRef(transaction_id=state.shown_candidates[0])
                new = new.model_copy(update={"transaction_ref": picked, "confirmation": None})
            elif answer is Confirmation.DECLINED:
                state.slots = state.slots.model_copy(update={"transaction_ref": None})
                state.transaction_ref_said, state.picked_candidate = None, None
                state.slot_turns.pop(SlotName.TRANSACTION_REF, None)
        self._apply_slots(turn, new, extraction.customer_claims)
        return False

    def _reject_identified(self, turn: _Turn, new: Slots, claims: list[str]) -> None:
        """Contract 15: "ese no es" at the block offer replaces the transaction (GATE-05 runs
        again on what the customer says now) and withdraws the offer for that card."""
        state = turn.state
        others = new.model_copy(update={"confirmation": None, "transaction_ref": None})
        self._apply_slots(turn, others, claims)
        ref = new.transaction_ref
        state.slots = state.slots.model_copy(update={"transaction_ref": ref, "confirmation": None})
        state.transaction_ref_said, state.picked_candidate = None, None
        state.shown_candidates = []
        if ref is not None:
            self._remember_said(state, ref, picked=None)
            self._mark(turn, SlotName.TRANSACTION_REF, claims)
        else:
            state.slot_turns.pop(SlotName.TRANSACTION_REF, None)
        state.block_offer, state.block_product_id = BlockOffer.NOT_OFFERED, None
        state.block_reasks = 0

    async def _answer_side_question(self, turn: _Turn, question: SideQuestion) -> None:
        """Contract 14: the answer goes first in the reply; the turn goes on after it."""
        answer: Prefix
        state = turn.state
        if question is SideQuestion.CASE_STATUS:
            answer = await self._case_status(turn)
        elif question is SideQuestion.OTHER:
            answer = [(SIDE_TEMPLATES[question], {})]
            if not state.unsupported_offered:  # contract 20: once per conversation
                answer.append(("offer_transfer", {}))
                state.unsupported_offered = True
        elif question is SideQuestion.FLOW_HELP:
            searching = (
                turn.asked_before is ClarifyTarget.TRANSACTION_REF
                or state.last_transaction_id is None
            )
            answer = [(SIDE_TEMPLATES[question] if searching else "side_flow_help", {})]
        else:
            answer = [(SIDE_TEMPLATES[question], {})]
        turn.side_question = question
        turn.prefix[0:0] = answer

    async def _case_status(self, turn: _Turn) -> Prefix:
        """The customer's own cases: the one whose reference they typed (``get_case``), else the
        most recent ones. Nothing is read without a session: the answer is to log in."""
        if turn.session is None:
            return [("ask_authentication", {})]
        assert turn.tools is not None
        typed = CASE_REF.search(turn.message)
        cases: list[CaseRecord]
        if typed is not None:
            try:
                case = await self._io(turn.tools.get_case, typed.group(0).upper())
            except AccessDeniedError:  # GATE-04: someone else's or none, the same answer
                return [("side_case_not_found", {})]
            if case.status is CaseStatus.DRAFT:  # never shown to a customer
                return [("side_case_not_found", {})]
            cases = [case]
        else:
            shown = [c for c in turn.records.cases if c.status is not CaseStatus.DRAFT]
            cases = sorted(shown, key=lambda c: c.created_at, reverse=True)[:MAX_CASES_SHOWN]
        if not cases:
            return [("side_no_cases", {})]
        language = self._language(turn)
        return [
            (
                "side_case_status",
                {
                    "case_ref": case.case_id,
                    "status": self._templates.label("case_status", case.status.value, language),
                },
            )
            for case in cases
        ]

    def _correct(self, turn: _Turn, new: Slots, claims: list[str]) -> bool:
        """Contract 6: a declined summary is a proposed correction (corrections.py)."""
        state = turn.state
        identified = self._find(turn.records.pool, state.last_transaction_id)
        correction = apply_declined_correction(state.slots, new, identified)
        if correction.clarify_target is ClarifyTarget.REASON_CODE:
            state.slots = correction.slots
            self._count(state, ClarifyTarget.REASON_CODE)
            self._new_reply(turn).add("clarify_reason_code")
            state.pending, state.pending_target = Pending.CLARIFY, ClarifyTarget.REASON_CODE
            turn.reply_kind = "clarify:reason_code"
            turn.outcome = Outcome.CLARIFY
            return True
        baseline = state.slots.model_copy(update={"confirmation": None})
        if correction.slots == baseline:
            # Nothing to correct with: ask which detail is wrong (ask_correction).
            state.slots = state.slots.model_copy(update={"confirmation": Confirmation.DECLINED})
            return False
        for name in SlotName:
            if getattr(correction.slots, name.value) != getattr(baseline, name.value):
                self._mark(turn, name, claims)
        if correction.rematch and new.transaction_ref is not None:
            self._remember_said(state, new.transaction_ref, picked=None)
        elif correction.rematch and correction.slots.transaction_ref is not None:
            amount = correction.slots.transaction_ref.amount
            said = state.transaction_ref_said or TransactionRef(amount=amount)
            state.transaction_ref_said = said.model_copy(update={"amount": amount})
            state.picked_candidate = None
        state.slots = correction.slots
        return False

    def _apply_slots(self, turn: _Turn, new: Slots, claims: list[str]) -> bool:
        """Merge the slots of this message; True if any changed. A change after the summary
        clears the confirmation (contract 5)."""
        state = turn.state
        updates: dict[str, object] = {}
        for name in SlotName:
            if name in (SlotName.CONFIRMATION, SlotName.TRANSACTION_REF):
                continue
            value = getattr(new, name.value)
            if value is not None and value != getattr(state.slots, name.value):
                updates[name.value] = value
        if new.transaction_ref is not None:
            merged = self._merge_reference(state, new.transaction_ref)
            if merged != state.slots.transaction_ref:
                updates[SlotName.TRANSACTION_REF.value] = merged
        if not updates:
            return False
        if state.slots.confirmation is not None:
            updates["confirmation"] = None
        state.slots = state.slots.model_copy(update=updates)
        for key in updates:
            if key != "confirmation":
                self._mark(turn, SlotName(key), claims)
        return True

    def _merge_reference(self, state: ConversationState, ref: TransactionRef) -> TransactionRef:
        if ref.transaction_id is not None:
            picked = (
                state.shown_candidates.index(ref.transaction_id) + 1
                if ref.transaction_id in state.shown_candidates
                else None
            )
            self._remember_said(state, ref, picked)
            return ref
        self._remember_said(state, ref, picked=None)
        return self._merged_reference(state, ref)

    @staticmethod
    def _merged_reference(state: ConversationState, ref: TransactionRef) -> TransactionRef:
        if ref.transaction_id is not None:
            return ref
        current = state.slots.transaction_ref
        replaced = {"transaction_id"}
        if ref.amount is not None:  # a new amount brings its own qualifier and currency
            replaced |= {"amount_approximate", "amount_currency"}
        if ref.transaction_date is not None:  # a day replaces a period, and the other way round
            replaced |= {"date_from", "date_to"}
        if ref.date_from is not None:
            replaced |= {"transaction_date"}
        base = current.model_dump(exclude=replaced, exclude_none=True) if current else {}
        base.update(ref.model_dump(exclude_none=True))
        return TransactionRef.model_validate(base)

    @staticmethod
    def _with_currency(turn: _Turn, slots: Slots) -> Slots:
        """The currency the customer named with an amount in this message, for display only
        (COM-08); "pesos" is the peso of the customer's country."""
        ref = slots.transaction_ref
        if ref is None or ref.amount is None:
            return slots
        currency = currency_said(turn.message, turn.records.country)
        if currency is None:
            return slots
        return slots.model_copy(
            update={"transaction_ref": ref.model_copy(update={"amount_currency": currency})}
        )

    def _informative(self, state: ConversationState, new: Slots) -> bool:
        """Contract 19: the message answers the question or adds or changes a detail."""
        if new.confirmation in (
            Confirmation.CONFIRMED,
            Confirmation.DECLINED,
            Confirmation.WITHDRAWN,
        ):
            return True
        for name in SlotName:
            if name in (SlotName.CONFIRMATION, SlotName.TRANSACTION_REF):
                continue
            value = getattr(new, name.value)
            if value is not None and value != getattr(state.slots, name.value):
                return True
        ref = new.transaction_ref
        return ref is not None and self._merged_reference(state, ref) != state.slots.transaction_ref

    @staticmethod
    def _remember_said(state: ConversationState, ref: TransactionRef, picked: int | None) -> None:
        """What the customer said about the transaction: descriptors, an ID only if typed."""
        said = ref.model_dump(exclude_none=True)
        if picked is not None:
            said.pop("transaction_id", None)
        if picked is None and "transaction_id" not in said and state.transaction_ref_said:
            said = {
                **state.transaction_ref_said.model_dump(
                    exclude={"transaction_id"}, exclude_none=True
                ),
                **said,
            }
        state.transaction_ref_said = TransactionRef.model_validate(said) if said else None
        state.picked_candidate = picked

    def _merge_flags(
        self, state: ConversationState, flags: ConversationFlags, claims: list[str]
    ) -> None:
        sticky = ("human_requested", "account_takeover_reported", "legal_or_vulnerability")
        updates: dict[str, bool] = {"authentication_declined": flags.authentication_declined}
        for name in sticky:
            raised = getattr(flags, name)
            if raised and not getattr(state.flags, name) and claims:
                state.evidence_claims[name] = list(claims)
            updates[name] = getattr(state.flags, name) or raised
        state.flags = ConversationFlags(**updates)

    def _mark(self, turn: _Turn, slot: SlotName, claims: list[str]) -> None:
        turn.state.slot_turns[slot] = turn.state.turn_index
        if claims:
            turn.state.evidence_claims[slot.value] = list(claims)

    # ------------------------------------------------------------------ evaluation

    async def _evaluate_and_act(self, turn: _Turn) -> None:
        request = await self._request(turn)
        with self._stage(turn, "policy"):
            decision = self._engine.evaluate(request)
        turn.decisions.append(decision)
        await self._act(turn, request, decision)

    async def _request(self, turn: _Turn) -> PolicyRequest:
        state, records, session = turn.state, turn.records, turn.session
        pool = list(records.pool)
        violation = False
        ref = state.slots.transaction_ref
        if (
            session is not None
            and ref is not None
            and ref.transaction_id is not None
            and all(t.transaction_id != ref.transaction_id for t in pool)
        ):
            assert turn.tools is not None
            try:
                pool = await self._io(turn.tools.transaction_candidates, ref.transaction_id)
            except AccessDeniedError:
                violation = True  # GATE-04: neither confirmed nor denied
        current = 1 if state.slots.reason_code is ReasonCode.UNRECOGNIZED else 0
        counters = state.counters.model_copy(
            update={"unrecognized_transactions": len(state.unrecognized_ids) + current}
        )
        return PolicyRequest(
            now=self._clock(),
            as_of=self._config.as_of,
            conversation_id=state.conversation_id,
            detected_language=turn.detected_language,
            language_ambiguous=turn.language_ambiguous,
            session=session,
            slots=state.slots,
            flags=state.flags,
            signals=turn.signals or ModelSignals(source=ModelSource.UNAVAILABLE),
            input_guard=turn.guard,
            counters=counters,
            customer=records.customer if session else None,
            ownership_violation=violation,
            transaction_candidates=pool if session else [],
            products=records.products if session else [],
            cases=records.cases if session else [],
            tool_results=list(state.tool_results),
        )

    async def _act(self, turn: _Turn, request: PolicyRequest, decision: PolicyDecision) -> None:
        state = turn.state
        state.last_transaction_id = decision.transaction_id
        rules = decision.triggered_rules
        if decision.card_already_blocked and not state.card_already_blocked_told:
            product = self._product_of(turn, decision.transaction_id)
            # Contract 22: a card ACT-03 blocked in this conversation was announced already.
            if product is not None and product.product_id not in state.blocked_here:
                turn.prefix.append(
                    ("card_already_blocked", {"product": product.product_number_masked})
                )
            state.card_already_blocked_told = True
            state.block_offer = BlockOffer.DONE
        if (
            decision.card_product_id is not None
            and decision.outcome is not Outcome.REFUSE
            and "ESC-05" not in rules
            and state.block_offer is BlockOffer.NOT_OFFERED
        ):
            # Contracts 5 and 8: the block is confirmed first, on its own; not a clarification.
            state.block_offer, state.block_product_id = BlockOffer.OFFERED, decision.card_product_id
            state.pending = Pending.BLOCK_OFFER
            self._offer_block(turn)
            return
        if decision.outcome is Outcome.CLARIFY:
            self._clarify(turn, request, decision)
        elif decision.outcome is Outcome.INFORM:
            self._inform(turn, decision)
        elif decision.outcome is Outcome.REFUSE:
            self._new_reply(turn).add("refuse")
            turn.reply_kind, turn.outcome = "refuse", Outcome.REFUSE
            state.slots = state.slots.model_copy(update={"transaction_ref": None})
            state.transaction_ref_said, state.picked_candidate = None, None
        elif decision.outcome is Outcome.RESOLVE:
            await self._resolve(turn, request, decision)
        else:
            await self._escalate(turn, request, decision)

    def _clarify(self, turn: _Turn, request: PolicyRequest, decision: PolicyDecision) -> None:
        state = turn.state
        target = decision.clarify_target
        assert target is not None
        reply = self._new_reply(turn)
        turn.outcome = Outcome.CLARIFY
        turn.reply_kind = f"clarify:{target.value}"
        state.pending, state.pending_target = Pending.CLARIFY, target
        first_summary = target is ClarifyTarget.CONFIRMATION and request.slots.confirmation is None
        repeated = turn.side_only and target is turn.asked_before  # contract 14
        shown: list[str] = []
        if target is ClarifyTarget.AUTHENTICATION:
            reconfirm = (
                state.slots.confirmation is not None or state.block_offer is BlockOffer.OFFERED
            )
            reconfirm = reconfirm or state.last_reply_kind in ("summary", "clarify:confirmation")
            # Contract 11: a confirmation from before the expiry no longer counts.
            reply.add("session_expired_reconfirm" if reconfirm else "ask_authentication")
            state.slots = state.slots.model_copy(update={"confirmation": None})
            if not repeated:
                state.counters = state.counters.model_copy(
                    update={"authentication_attempts": state.counters.authentication_attempts + 1}
                )
        elif target is ClarifyTarget.LANGUAGE:
            reply.add("ask_language")
            state.counters = state.counters.model_copy(
                update={"language_clarifications": state.counters.language_clarifications + 1}
            )
        elif first_summary:
            txn = self._find(request.transaction_candidates, decision.transaction_id)
            product = self._product_of(turn, decision.transaction_id)
            assert txn is not None and product is not None and decision.reason_code is not None
            reply.summary(txn, product, decision.reason_code)
            state.pending, state.pending_target = Pending.SUMMARY, None
            turn.reply_kind = "summary"
        else:
            items, shown = self._clarification(turn, request, decision, target)
            text = self._render(reply, items)
            if state.last_clarify == (target, text):  # contract 18: never twice in a row
                items = self._variant(turn, items)
                text = self._render(reply, items)
            for template_id, values in items:
                reply.add(template_id, **values)
            turn.clarify_key = (target, text)
        counted = target not in NOT_COUNTED and not first_summary and not repeated
        if counted:
            self._count(state, target)
        state.pending_counted = counted or (repeated and turn.still_counted)
        if decision.duplicate_reason_reask:
            state.counters = state.counters.model_copy(update={"duplicate_reason_reasked": True})
        state.shown_candidates = shown
        if target in SUMMARY_TARGETS or decision.duplicate_reason_reask or first_summary:
            state.slots = state.slots.model_copy(update={"confirmation": None})  # used

    def _clarification(
        self, turn: _Turn, request: PolicyRequest, decision: PolicyDecision, target: ClarifyTarget
    ) -> tuple[Prefix, list[str]]:
        """The templates of a clarification, and the candidates it shows."""
        reply = turn.reply
        assert reply is not None
        if target is ClarifyTarget.CONFIRMATION:
            return [("clarify_confirmation", {})], []
        if target is ClarifyTarget.CORRECTION:
            return [("ask_correction", {})], []
        if target is ClarifyTarget.TRANSACTION_REF and decision.candidate_transaction_ids:
            candidates = [
                txn
                for tid in decision.candidate_transaction_ids
                if (txn := self._find(request.transaction_candidates, tid)) is not None
            ]
            header = "choose_transaction"
            if decision.transaction_search is TransactionSearch.RELAXED:
                header = "choose_relaxed_one" if len(candidates) == 1 else "choose_relaxed_many"
            products = {p.product_id: p for p in turn.records.products}
            items: Prefix = [(header, {}), *reply.candidate_lines(candidates, products)]
            return items, [t.transaction_id for t in candidates]
        if target is ClarifyTarget.TRANSACTION_REF:
            return self._search_clarification(turn, decision), []
        if target is ClarifyTarget.DUPLICATE_REF:
            twin = self._find(request.transaction_candidates, decision.duplicate_transaction_id)
            assert twin is not None
            turn.state.pending_duplicate_id = twin.transaction_id
            values = {
                "transaction_date": format_date(twin.transaction_date),
                "amount": reply.amount(twin),
            }
            return [("clarify_duplicate_ref", values)], []
        if target is ClarifyTarget.EXPECTED_AMOUNT:
            txn = self._find(request.transaction_candidates, decision.transaction_id)
            return [("clarify_expected_amount", {"currency": txn.currency if txn else "USD"})], []
        return [(f"clarify_{target.value}", {})], []

    def _search_clarification(self, turn: _Turn, decision: PolicyDecision) -> Prefix:
        """GATE-05 without candidates to show (contract 18)."""
        ref = turn.state.slots.transaction_ref
        ask_for = decision.ask_for
        if decision.transaction_search is TransactionSearch.NO_MATCH and ask_for is not None:
            # The engine reports no match only on details the customer gave: never empty.
            return [
                ("no_match", {"known": self._searched(turn, ref)}),
                ("ask_transaction_detail", {"detail": self._detail(turn, ref, ask_for)}),
            ]
        if decision.transaction_search is TransactionSearch.TOO_MANY and ask_for is not None:
            return [("ask_many_detail", {"detail": self._detail(turn, ref, ask_for)})]
        return [("clarify_transaction_ref", {})]

    def _variant(self, turn: _Turn, items: Prefix) -> Prefix:
        """The second wording of the clarification sent the turn before (contract 18)."""
        first, values = items[0]
        if first in SEARCH_ASKS or first == "ask_transaction_detail":
            ref = turn.state.slots.transaction_ref
            known = self._known(turn, ref)
            if not known:
                return [("clarify_transaction_ref_again", {})]
            given = given_details(ref, self._categories)
            missing = [name for name in TransactionField if name not in given]
            language = self._language(turn)
            labels = [self._templates.label("detail", name.value, language) for name in missing]
            joiner = f" {self._templates.label('misc', 'or', language)} "
            wanted = joiner.join(labels) or self._templates.label(
                "detail", "merchant_statement", language
            )
            return [("clarify_transaction_ref_known", {"known": known, "missing": wanted})]
        variant = VARIANT_TEMPLATES.get(first)
        return [(variant, values), *items[1:]] if variant else items

    def _render(self, reply: Reply, items: Prefix) -> str:
        return "\n\n".join(
            self._templates.render(template_id, reply.language, **values)
            for template_id, values in items
        )

    def _detail(self, turn: _Turn, ref: TransactionRef | None, ask_for: TransactionField) -> str:
        everything = len(given_details(ref, self._categories)) == len(TransactionField)
        key = "merchant_statement" if everything else ask_for.value
        return self._templates.label("detail", key, self._language(turn))

    def _searched(self, turn: _Turn, ref: TransactionRef | None) -> str:
        """What GATE-05 searched with, in the customer's own details: "en El Buen Sabor por
        unos USD 40,00 del 16/06/2026", or "por unos 40" when no currency was named."""
        language = self._language(turn)
        parts: list[str] = []
        for name, value in self._details(turn, ref):
            period = name is TransactionField.DATE and ref is not None and ref.date_from
            label = "" if period else f"{self._templates.label('search', name.value, language)} "
            parts.append(f"{label}{value}")
        return " ".join(parts)

    def _known(self, turn: _Turn, ref: TransactionRef | None) -> str:
        """The customer's details as a list: "El Buen Sabor, unos USD 40,00"."""
        return ", ".join(value for _, value in self._details(turn, ref))

    def _details(
        self, turn: _Turn, ref: TransactionRef | None
    ) -> list[tuple[TransactionField, str]]:
        """The details the customer gave, formatted for the customer's locale."""
        if ref is None:
            return []
        details: list[tuple[TransactionField, str]] = []
        if ref.merchant:
            details.append((TransactionField.MERCHANT, ref.merchant))
        if ref.amount is not None:
            locale = self._locale(turn)
            number: str = (
                format_amount(ref.amount, ref.amount_currency, locale)
                if ref.amount_currency
                else plain_number(ref.amount, locale)  # never formatted without its code
            )
            if ref.amount_approximate:
                language = self._language(turn)
                number = f"{self._templates.label('search', 'approximately', language)} {number}"
            details.append((TransactionField.AMOUNT, number))
        if ref.transaction_date is not None:
            details.append((TransactionField.DATE, format_date(ref.transaction_date)))
        elif ref.date_from is not None and ref.date_to is not None:
            details.append((TransactionField.DATE, self._period(turn, ref.date_from, ref.date_to)))
        return details

    def _period(self, turn: _Turn, first: date, last: date) -> str:
        """A period of days as the customer gave it: "del 15/06/2026 al 17/06/2026"."""
        language, label = self._language(turn), self._templates.label
        start, end = label("search", "range_from", language), label("search", "range_to", language)
        return f"{start} {format_date(first)} {end} {format_date(last)}"

    def _inform(self, turn: _Turn, decision: PolicyDecision) -> None:
        reason = decision.inform_reason
        assert reason is not None
        reply = self._new_reply(turn)
        if decision.existing_case_id is not None:
            case = next(c for c in turn.records.cases if c.case_id == decision.existing_case_id)
            reply.duplicate_case(case)
        else:
            reply.add(INFORM_TEMPLATES[reason])
            reply.add("offer_transfer")
        turn.reply_kind, turn.outcome = f"inform:{reason.value}", Outcome.INFORM
        self._close_transaction(turn.state, decision, Outcome.INFORM)

    async def _resolve(self, turn: _Turn, request: PolicyRequest, decision: PolicyDecision) -> None:
        tools = turn.tools
        assert tools is not None and decision.transaction_id and decision.reason_code
        assert decision.tier is not None
        with self._stage(turn, "tools"):
            result = await self._io(
                tools.create_case, decision.transaction_id, decision.reason_code, decision.tier
            )
        self._record_tool(turn, result)
        if result.status is ToolStatus.SUCCESS and result.verified and result.record_id:
            self._new_reply(turn).add("case_created", case_ref=result.record_id)
            turn.reply_kind, turn.outcome = "case_created", Outcome.RESOLVE
            self._close_transaction(turn.state, decision, Outcome.RESOLVE)
            return
        # Failed or unverified (COM-04): evaluate again with the result, ESC-10 escalates.
        retry = request.model_copy(update={"tool_results": list(turn.state.tool_results)})
        decision = self._engine.evaluate(retry)
        turn.decisions.append(decision)
        await self._escalate(turn, retry, decision)

    async def _escalate(
        self, turn: _Turn, request: PolicyRequest, decision: PolicyDecision
    ) -> None:
        state = turn.state
        questions: list[str] = []
        offered = decision.card_product_id or state.block_product_id
        if state.block_offer is BlockOffer.OFFERED or (
            decision.card_product_id and state.block_offer is BlockOffer.NOT_OFFERED
        ):
            # Contract 7: ESC-05 is honored at once; the block goes to the agent.
            product = self._product_by_id(turn, offered)
            last4 = product.product_number_masked if product else "the card"
            questions.append(
                f"The card block of {last4} was not confirmed because the customer asked for "
                "a human: offer it."
            )
        with self._stage(turn, "handoff"):
            packet = self._builder.build(
                request=request,
                decision=decision,
                language=state.language or Language.ES,
                customer_claims=list(state.claims),
                actions_taken=list(state.tool_results),
                open_questions=questions,
                transcript_ref=state.conversation_id,
                evidence_claims={
                    name: [c for c in claims if c in state.claims]
                    for name, claims in state.evidence_claims.items()
                },
                slot_turns=dict(state.slot_turns),
                transaction_ref_said=state.transaction_ref_said,
                picked_candidate=state.picked_candidate,
            )
            assert turn.tools is not None
            transfer = await self._io(turn.tools.transfer_to_human, packet)
        self._record_tool(turn, transfer)
        authenticated = self._authenticated(turn)
        reply = self._new_reply(turn)
        if "ESC-10" in decision.triggered_rules:
            reply.add("tool_failure")
        turn.outcome = Outcome.ESCALATE
        if transfer.status is ToolStatus.SUCCESS:
            reply.add("handoff" if authenticated else "handoff_unauthenticated")
            turn.reply_kind = "handoff"
            turn.handoff_id = packet.handoff_id
            state.closed, state.handoff_id = True, packet.handoff_id
        else:
            if "tool_failure" not in reply.ids:
                reply.add("tool_failure")
            reply.add("offer_transfer")
            turn.reply_kind = "handoff_failed"
        if decision.transaction_id:
            state.outcomes[decision.transaction_id] = Outcome.ESCALATE

    # ------------------------------------------------------------------ actions

    async def _block_card(self, turn: _Turn) -> None:
        state = turn.state
        assert turn.tools is not None and state.block_product_id is not None
        with self._stage(turn, "tools"):
            result = await self._io(turn.tools.block_card, state.block_product_id)
        self._record_tool(turn, result)
        state.block_offer = BlockOffer.DONE
        if result.status is ToolStatus.SUCCESS and result.verified:
            state.blocked_here.add(state.block_product_id)
            product = self._product_by_id(turn, state.block_product_id)
            masked = product.product_number_masked if product else None
            if masked:
                turn.prefix.append(("card_blocked", {"product": masked}))

    def _offer_block(self, turn: _Turn) -> None:
        """ACT-03 confirmation (contracts 5, 15): the charge it is about, then the offer."""
        state = turn.state
        product = self._product_by_id(turn, state.block_product_id)
        assert product is not None
        reply = self._new_reply(turn)
        txn = self._find(turn.records.pool, state.last_transaction_id)
        if txn is not None:
            reply.add(
                "identified_transaction",
                transaction_date=format_date(txn.transaction_date),
                merchant=reply.merchant(txn),
                amount=reply.amount(txn),
            )
        reply.add("confirm_block_card", product=product.product_number_masked)
        turn.reply_kind, turn.outcome = "block_offer", Outcome.CLARIFY

    def _record_tool(self, turn: _Turn, result: ToolResult) -> None:
        turn.tool_calls.append(result)
        if result.action is not ActionId.TRANSFER_TO_HUMAN:
            turn.state.tool_results.append(result)

    def _close_transaction(
        self, state: ConversationState, decision: PolicyDecision, outcome: Outcome
    ) -> None:
        if decision.transaction_id:
            state.outcomes[decision.transaction_id] = outcome
            if decision.reason_code is ReasonCode.UNRECOGNIZED:
                state.unrecognized_ids.add(decision.transaction_id)
        state.reset_transaction()

    # ------------------------------------------------------------------ failures

    async def _emergency(self, turn: _Turn, exc: Exception) -> None:
        """Contract 4: a handoff with the tool_failure notice; the trace records the error."""
        state = turn.state
        turn.error = f"{type(exc).__name__}: {exc}"[:500]
        turn.prefix.clear()
        evidence = Evidence(
            kind=EvidenceKind.TOOL_RESULT,
            name="orchestrator",
            value=type(exc).__name__,
            origin="Orchestrator",
        )
        decision = PolicyDecision(
            outcome=Outcome.ESCALATE,
            policy_version=self._config.policy_version,
            triggered_rules=["ESC-10"],
            authorized_actions=[ActionId.TRANSFER_TO_HUMAN],
            queue=Queue.DISPUTES,
            priority=Priority.NORMAL,
            evidence=[RuleEvidence(rule_id="ESC-10", evidence=[evidence])],
        )
        authenticated = turn.session is not None
        reply = Reply(self._templates, state.language or Language.ES, self._locale(turn))
        reply.add("tool_failure")
        turn.reply, turn.outcome, turn.reply_kind = reply, Outcome.ESCALATE, "tool_failure"
        try:
            request = PolicyRequest(
                now=self._clock(),
                as_of=self._config.as_of,
                conversation_id=state.conversation_id,
                session=turn.session,
            )
            packet = self._builder.build(
                request=request,
                decision=decision,
                language=state.language or Language.ES,
                customer_claims=list(state.claims),
                actions_taken=list(state.tool_results),
                open_questions=["The conversation stopped on an internal error: review it."],
                transcript_ref=state.conversation_id,
            )
            tools = turn.tools or self._tools(turn.session, state.conversation_id)
            transfer = await self._io(tools.transfer_to_human, packet)
            if transfer.status is ToolStatus.SUCCESS:
                reply.add("handoff" if authenticated else "handoff_unauthenticated")
                state.closed, state.handoff_id = True, packet.handoff_id
                turn.handoff_id = packet.handoff_id
                return
        except Exception as failure:
            turn.error += f" | handoff failed: {type(failure).__name__}"
        reply.add("offer_transfer")

    # ------------------------------------------------------------------ reply and trace

    async def _finish_reply(self, turn: _Turn) -> str:
        reply = turn.reply or self._new_reply(turn)
        if turn.prefix:
            ids, texts = list(reply.ids), list(reply.texts)
            reply.ids, reply.texts = [], []
            for template_id, values in turn.prefix:
                if not self._redundant(template_id, ids):
                    reply.add(template_id, **values)
            flow_help = turn.side_only and turn.side_question is SideQuestion.FLOW_HELP
            if not (flow_help and ids and set(ids) <= TRANSACTION_ASKS):
                reply.ids += ids  # the flow_help answer already asks for the details
                reply.texts += texts
        try:
            reply.check(self._authenticated(turn) or turn.session is not None)
        except Exception as exc:
            if turn.error is None:
                await self._emergency(turn, exc)
                reply = turn.reply or reply
        text = reply.text
        state = turn.state
        if (
            turn.llm_ok
            and turn.deadline is not None
            and turn.context is not None
            and state.language is not None
            and "ask_language" not in reply.ids
            and not set(reply.ids) & LOGIN_IDS  # contract 17
            and turn.side_question is not SideQuestion.OTHER  # nothing to accept (contract 21)
            and turn.reply_kind != "closing"  # the closing is complete as it is
        ):
            context = turn.context.model_copy(update={"language": state.language})
            # Contract 21: full sentences only where they add something.
            full = state.turn_index == 0 or turn.outcome in BAD_NEWS or "no_match" in reply.ids
            try:
                with self._stage(turn, "connect"):
                    text = await self._llm.connect(
                        text,
                        turn.message,
                        context,
                        turn.deadline,
                        brief=not full,
                        previous=state.connect_sentences[-MAX_PREVIOUS_SENTENCES:],
                    )
            except Exception:
                text = reply.text
            if reply.text in text:
                before, after = text.split(reply.text, 1)
                state.connect_sentences += [s for s in (before.strip(), after.strip()) if s]
        return text

    def _trace(self, turn: _Turn, calls: list[Any], total_ms: float) -> None:
        state = turn.state
        tokens = sum(
            (c.input_tokens or 0) + (c.output_tokens or 0) for c in calls if c.provider == "openai"
        )
        state.tokens_used += tokens
        state.last_reply_kind = turn.reply_kind
        state.last_clarify = turn.clarify_key
        state.last_outcome = turn.outcome
        cost: Decimal | None = estimate_cost(calls, self._config.rates)
        trace = TraceRecord(
            trace_id=turn.trace_id,
            conversation_id=state.conversation_id,
            session_id=turn.session.session_id if turn.session else None,
            turn_index=state.turn_index,
            created_at=turn.created_at,
            language=state.language,
            message=turn.message,
            input_guard=turn.guard,
            model_calls=list(calls),
            signals=turn.signals,
            decisions=list(turn.decisions),
            tool_calls=list(turn.tool_calls),
            outcome=turn.outcome,
            reply_kind=turn.reply_kind or None,
            side_question=turn.side_question,
            handoff_id=turn.handoff_id,
            stage_latencies_ms=dict(turn.stages),
            total_latency_ms=total_ms,
            estimated_cost_usd=cost,
            policy_version=self._config.policy_version,
            error=turn.error,
        )
        try:
            self._tracer.record(trace)
        except Exception:  # the reply must not fail because the trace could not be stored
            turn.error = turn.error or "trace not stored"

    # ------------------------------------------------------------------ helpers

    def _new_reply(self, turn: _Turn) -> Reply:
        turn.reply = Reply(self._templates, self._language(turn), self._locale(turn))
        return turn.reply

    @staticmethod
    def _language(turn: _Turn) -> Language:
        return turn.state.language or guess_language(turn.message) or Language.ES

    def _locale(self, turn: _Turn) -> Locale:
        return locale_for(self._language(turn), turn.records.country)

    @staticmethod
    def _redundant(template_id: str, reply_ids: list[str]) -> bool:
        """A side answer the rest of the reply already covers: the same template, a login
        request next to another one, or a transfer offer next to a handoff."""
        if template_id in reply_ids:
            return True
        if template_id == "ask_authentication":
            return bool(set(reply_ids) & (LOGIN_IDS | HANDOFF_IDS))
        return template_id == "offer_transfer" and bool(set(reply_ids) & HANDOFF_IDS)

    def _authenticated(self, turn: _Turn) -> bool:
        return any(
            gate.gate_id == "GATE-02" and gate.passed
            for decision in turn.decisions
            for gate in decision.gates_evaluated
        )

    @staticmethod
    def _find(
        records: list[TransactionRecord], transaction_id: str | None
    ) -> TransactionRecord | None:
        return next((t for t in records if t.transaction_id == transaction_id), None)

    def _product_by_id(self, turn: _Turn, product_id: str | None) -> ProductRecord | None:
        return next((p for p in turn.records.products if p.product_id == product_id), None)

    def _product_of(self, turn: _Turn, transaction_id: str | None) -> ProductRecord | None:
        txn = self._find(turn.records.pool, transaction_id)
        return self._product_by_id(turn, txn.product_id) if txn else None

    @staticmethod
    def _uncount(state: ConversationState, target: ClarifyTarget) -> None:
        by_slot = dict(state.counters.clarifications_by_slot)
        by_slot[target] = max(0, by_slot.get(target, 0) - 1)
        state.counters = state.counters.model_copy(
            update={
                "clarifications_by_slot": by_slot,
                "total_clarifications": max(0, state.counters.total_clarifications - 1),
            }
        )

    @staticmethod
    def _count(state: ConversationState, target: ClarifyTarget) -> None:
        by_slot = dict(state.counters.clarifications_by_slot)
        by_slot[target] = by_slot.get(target, 0) + 1
        state.counters = state.counters.model_copy(
            update={
                "clarifications_by_slot": by_slot,
                "total_clarifications": state.counters.total_clarifications + 1,
            }
        )

    @contextmanager
    def _stage(self, turn: _Turn, name: str) -> Iterator[None]:
        started = time.perf_counter()
        try:
            yield
        finally:
            turn.stages[name] = turn.stages.get(name, 0.0) + (time.perf_counter() - started) * 1000

    @staticmethod
    async def _io(fn: Callable[..., T], *args: Any) -> T:
        """Blocking database work runs in a thread (architecture §3)."""
        return await asyncio.to_thread(fn, *args)
