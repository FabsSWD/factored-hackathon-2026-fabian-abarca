"""Fault injection and signal capture around the Orchestrator's modules (M18).

The harness registers a ``FaultPlan`` per conversation before it starts. The wrappers read the
conversation of the turn in progress from a context variable that ``HarnessOrchestrator`` sets,
so a fault touches only the conversation of the case that asks for it:

- ``tool_write_failure`` (ESC-10): ACT-02 fails after its retries, as the Tool Layer reports it;
- ``models_down`` (ESC-11): ``extract`` and ``connect`` get no answer and Kev is unavailable.

The wrappers also keep, per conversation and turn, what ``extract`` returned and what Kev
returned, so Kev and the fallback derived from the extraction are compared on the same turns.
Nothing is changed in what the modules return when no fault is planned.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from app.contracts import (
    ActionId,
    ExtractionResult,
    LLMContext,
    ModelSignals,
    ModelSource,
    ReasonCode,
    SessionContext,
    Tier,
    ToolResult,
    ToolStatus,
)
from app.deadline import Deadline
from app.interfaces import DecisionClient, LLMAdapter, ToolLayer
from app.llm_adapter.adapter import ExtractionUnavailableError
from app.llm_adapter.client import LLMError
from app.llm_adapter.signals import RuleBasedSignalDetector
from app.orchestrator.service import Orchestrator, TurnResult

TurnKey = tuple[str, int]  # (conversation_id, turn index)
CURRENT_TURN: ContextVar[TurnKey | None] = ContextVar("harness_turn", default=None)
INJECTED = "injected by the M18 harness"


@dataclass(frozen=True)
class FaultPlan:
    tool_write_failure: bool = False  # ESC-10
    models_down: bool = False  # ESC-11

    @property
    def any(self) -> bool:
        return self.tool_write_failure or self.models_down


@dataclass
class Capture:
    """What the models returned on each turn, for the Kev evaluation."""

    extractions: dict[TurnKey, ExtractionResult] = field(default_factory=dict)
    kev: dict[TurnKey, ModelSignals] = field(default_factory=dict)


@dataclass
class FaultRegistry:
    plans: dict[str, FaultPlan] = field(default_factory=dict)

    def register(self, conversation_id: str, plan: FaultPlan) -> None:
        self.plans[conversation_id] = plan

    def plan(self, conversation_id: str | None) -> FaultPlan:
        return self.plans.get(conversation_id or "", FaultPlan())


def _current() -> TurnKey | None:
    return CURRENT_TURN.get()


class HarnessLLM:
    """``LLMAdapter`` with the models-down fault and the capture of each extraction."""

    def __init__(self, inner: LLMAdapter, faults: FaultRegistry, capture: Capture) -> None:
        self._inner, self._faults, self._capture = inner, faults, capture

    async def extract(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ExtractionResult:
        key = _current()
        if self._faults.plan(key[0] if key else None).models_down:
            fallback = ExtractionResult(flags=RuleBasedSignalDetector().detect(message))
            self._keep(key, fallback)
            raise ExtractionUnavailableError(INJECTED, fallback)
        try:
            result = await self._inner.extract(message, context, deadline)
        except ExtractionUnavailableError as exc:
            self._keep(key, exc.fallback)
            raise
        self._keep(key, result)
        return result

    async def connect(
        self,
        templated_text: str,
        message: str,
        context: LLMContext,
        deadline: Deadline | None = None,
        *,
        brief: bool = False,
        previous: Sequence[str] = (),
    ) -> str:
        key = _current()
        if self._faults.plan(key[0] if key else None).models_down:
            raise LLMError(INJECTED)
        return await self._inner.connect(
            templated_text, message, context, deadline, brief=brief, previous=previous
        )

    def _keep(self, key: TurnKey | None, extraction: ExtractionResult) -> None:
        if key is not None:
            self._capture.extractions[key] = extraction


class HarnessDecision:
    """``DecisionClient`` with the models-down fault and the capture of Kev's signals."""

    def __init__(self, inner: DecisionClient, faults: FaultRegistry, capture: Capture) -> None:
        self._inner, self._faults, self._capture = inner, faults, capture

    async def signals(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ModelSignals:
        key = _current()
        if self._faults.plan(key[0] if key else None).models_down:
            signals = ModelSignals(source=ModelSource.UNAVAILABLE)
        else:
            signals = await self._inner.signals(message, context, deadline)
        if key is not None:
            self._capture.kev[key] = signals
        return signals


class FailingWriteTools:
    """A Tool Layer whose ACT-02 fails after its retries; everything else is the real one."""

    def __init__(self, inner: ToolLayer, attempts: int) -> None:
        self._inner, self._attempts = inner, attempts

    def create_case(self, transaction_id: str, reason_code: ReasonCode, tier: Tier) -> ToolResult:
        return ToolResult(
            action=ActionId.CREATE_CASE,
            status=ToolStatus.FAILED,
            verified=False,
            attempts=self._attempts,
            idempotency_key=f"{transaction_id}:{reason_code.value}",
            error=INJECTED,
            completed_at=datetime.now(UTC),
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def harness_tools(
    tools: Callable[[SessionContext | None, str], ToolLayer],
    faults: FaultRegistry,
    attempts: int,
) -> Callable[[SessionContext | None, str], ToolLayer]:
    """The Tool Layer factory with the write fault for the conversations that plan it."""

    def build(session: SessionContext | None, conversation_id: str) -> ToolLayer:
        layer = tools(session, conversation_id)
        if faults.plan(conversation_id).tool_write_failure:
            return FailingWriteTools(layer, attempts)
        return layer

    return build


class HarnessOrchestrator(Orchestrator):
    """The real Orchestrator; it only names the turn in progress for the wrappers."""

    def __init__(self, **parts: Any) -> None:
        super().__init__(**parts)
        self._turn_counts: dict[str, int] = {}

    async def handle_turn(
        self, conversation_id: str | None, message: str, token: str | None = None
    ) -> TurnResult:
        if conversation_id is None:
            return await super().handle_turn(conversation_id, message, token)
        index = self._turn_counts.get(conversation_id, 0)
        self._turn_counts[conversation_id] = index + 1
        mark = CURRENT_TURN.set((conversation_id, index))
        try:
            return await super().handle_turn(conversation_id, message, token)
        finally:
            CURRENT_TURN.reset(mark)
