"""Conversation state kept by the Orchestrator between turns (M12).

One state per ``conversation_id``. It holds what the policy engine needs across turns (the
accumulated slots, the clarification counters, the injection strikes, the transactions already
evaluated) and what the Orchestrator itself must remember (the question pending from the last
turn, the card block offer, the candidates shown, the claims behind each slot).

Prototype limitation: the store is in memory, in one process. A restart loses open
conversations; a multi-process deployment would need a shared store behind the same
``ConversationStore`` interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from app.contracts import (
    ClarifyTarget,
    ConversationCounters,
    ConversationFlags,
    Language,
    Outcome,
    SlotName,
    Slots,
    ToolResult,
    TransactionRef,
)


class Pending(StrEnum):
    """The question the last turn asked, which the next message answers."""

    NONE = "none"
    CLARIFY = "clarify"  # a CLARIFY target (slot, language, authentication, correction)
    BLOCK_OFFER = "block_offer"  # ACT-03 confirmation (COM-03)
    SUMMARY = "summary"  # ACT-02 summary (COM-03)


class BlockOffer(StrEnum):
    NOT_OFFERED = "not_offered"
    OFFERED = "offered"
    DECLINED = "declined"
    DONE = "done"  # executed (successfully or not), or the card was already blocked


@dataclass
class ConversationState:
    conversation_id: str
    customer_id: str | None = None  # bound by the first authenticated turn
    language: Language | None = None  # the conversation's language (COM-01)
    turn_index: int = 0
    slots: Slots = field(default_factory=Slots)
    slot_turns: dict[SlotName, int] = field(default_factory=dict)
    flags: ConversationFlags = field(default_factory=ConversationFlags)
    counters: ConversationCounters = field(default_factory=ConversationCounters)
    pending: Pending = Pending.NONE
    pending_target: ClarifyTarget | None = None
    pending_duplicate_id: str | None = None  # the twin shown in clarify_duplicate_ref
    shown_candidates: list[str] = field(default_factory=list)  # transaction IDs, in order
    transaction_ref_said: TransactionRef | None = None
    picked_candidate: int | None = None
    block_offer: BlockOffer = BlockOffer.NOT_OFFERED
    block_product_id: str | None = None
    block_reasks: int = 0  # unclear answers to the offer asked again (side questions excluded)
    card_already_blocked_told: bool = False
    claims: list[str] = field(default_factory=list)
    evidence_claims: dict[str, list[str]] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    unrecognized_ids: set[str] = field(default_factory=set)
    outcomes: dict[str, Outcome] = field(default_factory=dict)  # one per transaction
    tokens_used: int = 0
    last_transaction_id: str | None = None  # the transaction of the last decision
    last_reply_kind: str = ""
    # The clarification sent the turn before (target, text): never the same text twice.
    last_clarify: tuple[ClarifyTarget, str] | None = None
    pending_counted: bool = False  # the pending clarification counted toward ESC-09
    unsupported_offered: bool = False  # offer_transfer after side question "other": once
    connect_sentences: list[str] = field(default_factory=list)  # connecting sentences sent
    closed: bool = False  # handed off: automation ended
    handoff_id: str | None = None

    def reset_transaction(self) -> None:
        """After a final outcome for one transaction: the customer may dispute another one."""
        self.slots = Slots()
        self.slot_turns = {}
        self.pending = Pending.NONE
        self.pending_target = None
        self.pending_duplicate_id = None
        self.shown_candidates = []
        self.transaction_ref_said = None
        self.picked_candidate = None
        self.block_offer = BlockOffer.NOT_OFFERED
        self.block_product_id = None
        self.block_reasks = 0
        self.card_already_blocked_told = False
        self.counters = self.counters.model_copy(
            update={
                "clarifications_by_slot": {},
                "total_clarifications": 0,
                "duplicate_reason_reasked": False,
                "unresolved_contradiction": False,
            }
        )


class ConversationStore(Protocol):
    def get(self, conversation_id: str) -> ConversationState | None: ...

    def save(self, state: ConversationState) -> None: ...


class InMemoryConversationStore:
    def __init__(self) -> None:
        self._states: dict[str, ConversationState] = {}

    def get(self, conversation_id: str) -> ConversationState | None:
        return self._states.get(conversation_id)

    def save(self, state: ConversationState) -> None:
        self._states[state.conversation_id] = state
