"""M13 light load: 100 turns through the Orchestrator with latency doubles of the model services.

20 conversations run concurrently, 5 turns each (a dispute, its confirmation, a thank-you, a side
question and an answer without details), so the mix has summaries, RESOLVE, closings, side
answers and clarifications. The doubles sleep for a latency drawn from a profile; everything
else (Policy Engine, templates, composition, the in-memory bank of tests/orchestrator/fakes.py)
is real code. ``extract`` and Kev run in parallel; ``connect`` follows, unless disabled.

Used by tests/e2e/test_load.py (fast doubles) and scripts/load_test.py (measured latencies).
"""

from __future__ import annotations

import asyncio
import random
import statistics
import time
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

from app.contracts import (
    Confirmation,
    ExtractionResult,
    LLMContext,
    ModelSignals,
    ModelSource,
    ReasonCode,
    SideQuestion,
    TransactionRef,
)
from app.deadline import Deadline
from tests.orchestrator.fakes import REF_CAFE, ScriptedKev, ScriptedLLM, build_world, ext


@dataclass(frozen=True)
class Profile:
    """Seconds, uniform between the bounds."""

    name: str
    extract: tuple[float, float]
    connect: tuple[float, float]
    kev: tuple[float, float]


MEASURED = Profile("measured", extract=(2.0, 4.9), connect=(1.5, 2.5), kev=(0.2, 0.4))
"""Recorded with gpt-6-luna (extract@1.9.0, connect@1.2.0) and Kev on the deployment host."""
FAST = Profile("fast", extract=(0.05, 0.12), connect=(0.03, 0.06), kev=(0.01, 0.03))
"""The milestone's targets (p50 < 2 s, p95 < 5 s) are for fast doubles."""


class LatencyLLM(ScriptedLLM):
    def __init__(self, profile: Profile, rng: random.Random, connect_enabled: bool) -> None:
        super().__init__(tokens_per_call=0)
        self._profile, self._rng, self._connect_enabled = profile, rng, connect_enabled

    async def extract(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ExtractionResult:
        await asyncio.sleep(self._rng.uniform(*self._profile.extract))
        return await super().extract(message, context, deadline)

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
        if self._connect_enabled:  # LLM_CONNECT_ENABLED=false returns the template at once
            await asyncio.sleep(self._rng.uniform(*self._profile.connect))
        return templated_text


class LatencyKev(ScriptedKev):
    def __init__(self, profile: Profile, rng: random.Random) -> None:
        super().__init__()
        self._profile, self._rng = profile, rng

    async def signals(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ModelSignals:
        await asyncio.sleep(self._rng.uniform(*self._profile.kev))
        return ModelSignals(source=ModelSource.KEV)


@dataclass(frozen=True)
class LoadResult:
    profile: str
    connect_enabled: bool
    turns: int
    p50: float
    p95: float
    mean: float
    maximum: float
    wall: float
    outcomes: dict[str, int]


def percentile(values: Sequence[float], share: float) -> float:
    """Linear interpolation between closest ranks (the "type 7" method of app.audit.metrics)."""
    ordered = sorted(values)
    position = (len(ordered) - 1) * share
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


SCRIPT = [
    ("Cafe Sintetico me cobró 50 y había acordado 40", ext(
        transaction_ref=REF_CAFE, reason_code=ReasonCode.INCORRECT_AMOUNT,
        expected_amount=Decimal("40"))),
    ("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED)),
    ("gracias", ext()),
    ("¿y cuánto tarda?", ext(side=SideQuestion.TIMELINE)),
    ("no sé cuál otro", ext()),
]  # fmt: skip


def run_load(
    profile: Profile,
    *,
    connect_enabled: bool = True,
    conversations: int = 20,
    turns_each: int = 5,
    seed: int = 13,
) -> LoadResult:
    rng = random.Random(seed)
    world = build_world()
    world.orchestrator._llm = LatencyLLM(profile, rng, connect_enabled)
    world.orchestrator._decision = LatencyKev(profile, rng)
    llm = world.orchestrator._llm
    assert isinstance(llm, LatencyLLM)
    for message, extraction in SCRIPT:
        llm.script[message] = extraction
    # Each conversation is another customer disputing their own charge: one customer with 20
    # disputes would rightly hit ESC-02 (dispute velocity) depending on the interleaving.
    template = world.identity.sessions["token-cus-1"]
    for index in range(conversations):
        customer = f"CUS-LOAD-{index}"
        world.identity.sessions[f"token-load-{index}"] = template.model_copy(
            update={"session_id": f"SES-LOAD-{index}", "customer_id": customer}
        )
        card = world.bank.products[0].model_copy(
            update={"product_id": f"PRD-LOAD-{index}", "customer_id": customer}
        )
        world.bank.products.append(card)
        world.bank.transactions.append(
            world.bank.transactions[0].model_copy(
                update={
                    "transaction_id": f"TXN-LOAD-{index}",
                    "customer_id": customer,
                    "product_id": card.product_id,
                }
            )
        )
    latencies: list[float] = []
    outcomes: dict[str, int] = {}

    async def conversation(index: int) -> None:
        cid = f"CONV-LOAD-{index:03d}"
        for turn in range(turns_each):
            message, _ = SCRIPT[turn % len(SCRIPT)]
            if turn == 0:  # this conversation's own charge
                llm.script[f"{message} ({index})"] = ext(
                    transaction_ref=TransactionRef(transaction_id=f"TXN-LOAD-{index}"),
                    reason_code=ReasonCode.INCORRECT_AMOUNT,
                    expected_amount=Decimal("40"),
                )
                message = f"{message} ({index})"
            started = time.perf_counter()
            result = await world.orchestrator.handle_turn(cid, message, f"token-load-{index}")
            latencies.append(time.perf_counter() - started)
            key = result.outcome.value if result.outcome else "none"
            outcomes[key] = outcomes.get(key, 0) + 1

    async def main() -> float:
        started = time.perf_counter()
        await asyncio.gather(*(conversation(i) for i in range(conversations)))
        return time.perf_counter() - started

    wall = asyncio.run(main())
    return LoadResult(
        profile=profile.name,
        connect_enabled=connect_enabled,
        turns=len(latencies),
        p50=percentile(latencies, 0.50),
        p95=percentile(latencies, 0.95),
        mean=statistics.fmean(latencies),
        maximum=max(latencies),
        wall=wall,
        outcomes=dict(sorted(outcomes.items())),
    )
