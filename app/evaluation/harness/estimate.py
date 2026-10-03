"""Time and token estimate of an evaluation, before any model call (``--dry-run``).

Figures come from measured runs: ``extract`` 1,700-1,900 input tokens per call
(reports/m5_llm_extraction_evidence.json, grown with extract@1.12.0), ``connect`` about 400,
turn latency p50 4.8 s with connecting sentences and 3.1 s without (reports/
m13_load_results.json). Turns per conversation is an assumption; the run reports the real one.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.audit.cost import TokenRates

EXTRACT_INPUT, EXTRACT_OUTPUT = 1900, 300
CONNECT_INPUT, CONNECT_OUTPUT = 420, 90
TURN_SECONDS_CONNECT, TURN_SECONDS_QUIET = 4.8, 3.1
TURNS_PER_CASE = 5.0
BASELINE_OUTPUT = 400
BASELINE_SECONDS = 15.0
CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class Estimate:
    conversations: int
    turns: float
    input_tokens: int
    output_tokens: int
    baseline_input_tokens: int
    baseline_output_tokens: int
    minutes: float
    cost_usd: Decimal | None  # None: no token rates configured

    def lines(self) -> list[str]:
        cost = (
            "unknown (no LLM_*_USD_PER_MTOK rates)"
            if self.cost_usd is None
            else (f"about USD {self.cost_usd:.2f}")
        )
        return [
            f"conversations: {self.conversations} (about {self.turns:.0f} turns)",
            f"system tokens: about {self.input_tokens:,} in, {self.output_tokens:,} out",
            f"baseline tokens: about {self.baseline_input_tokens:,} in, "
            f"{self.baseline_output_tokens:,} out",
            f"time: about {self.minutes:.0f} minutes",
            f"cost: {cost}",
        ]


def estimate(
    cases: int,
    runs: int,
    connect_off_run: bool,
    baseline: bool,
    concurrency: int,
    baseline_prompt_chars: int,
    rates: TokenRates | None,
) -> Estimate:
    connect_runs, quiet_runs = runs, int(connect_off_run)
    turns_connect = cases * connect_runs * TURNS_PER_CASE
    turns_quiet = cases * quiet_runs * TURNS_PER_CASE
    turns = turns_connect + turns_quiet
    input_tokens = int(turns * EXTRACT_INPUT + turns_connect * CONNECT_INPUT)
    output_tokens = int(turns * EXTRACT_OUTPUT + turns_connect * CONNECT_OUTPUT)
    baseline_in = cases * (baseline_prompt_chars // CHARS_PER_TOKEN + 1000) if baseline else 0
    baseline_out = cases * BASELINE_OUTPUT if baseline else 0
    lanes = max(1, concurrency)
    seconds = (
        turns_connect * TURN_SECONDS_CONNECT
        + turns_quiet * TURN_SECONDS_QUIET
        + (cases * BASELINE_SECONDS if baseline else 0)
    ) / lanes
    seconds += 60 * (connect_runs + quiet_runs)  # seeding and start-up per run
    cost = None
    if rates is not None:
        total_in, total_out = input_tokens + baseline_in, output_tokens + baseline_out
        cost = (
            Decimal(total_in) * rates.input_usd_per_mtok
            + Decimal(total_out) * rates.output_usd_per_mtok
        ) / Decimal(1_000_000)
    return Estimate(
        conversations=cases * (connect_runs + quiet_runs),
        turns=turns,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        baseline_input_tokens=baseline_in,
        baseline_output_tokens=baseline_out,
        minutes=seconds / 60,
        cost_usd=cost,
    )
