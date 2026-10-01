"""Message retention, cost estimate, percentiles and metrics: pure functions."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.audit.cost import TokenRates, estimate_cost, rates_from_settings
from app.audit.masking import AuditMessageMode, mask_message, retain_message
from app.audit.metrics import compute_metrics, percentile
from app.contracts import Language, Outcome
from tests.audit.helpers import model_calls, trace
from tests.policy.conftest import evaluate, request

# --- Message retention -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Removed: e-mails, cards in groups, phones, documents.
        ("mi correo es ana.prueba+1@example.test", "mi correo es [email]"),
        ("tarjeta 4111 1111 1111 1111", "tarjeta ****1111"),
        ("tarjeta 4111-1111-1111-1111", "tarjeta ****1111"),
        ("llama al 8888-1234", "llama al [number]"),
        ("llama al +506 8888 1234", "llama al [telefono]"),
        ("llama al (11) 98765-4321", "llama al [number]"),
        ("pasaporte X1234567", "pasaporte [documento]"),
        ("soy X1234567", "soy [number]"),
        ("documento 42388496", "documento [documento]"),
        ("mi número es 42388496", "mi número es [number]"),
        # Kept: amounts, dates, aliases, references.
        ("cobro de COP 1.250.000,00", "cobro de COP 1.250.000,00"),
        ("cobro de USD 1,250.50", "cobro de USD 1,250.50"),
        ("el 17/06/2026", "el 17/06/2026"),
        ("del 2026-06-01, ref C2", "del 2026-06-01, ref C2"),
        ("alias C2 y C12", "alias C2 y C12"),
        ("me cobraron 400000 pesos", "me cobraron 400000 pesos"),
        ("TRX-20260616 no lo reconozco", "TRX-20260616 no lo reconozco"),
    ],
)
def test_masking(text: str, expected: str) -> None:
    assert mask_message(text) == expected


def test_retention_modes() -> None:
    text = "mi número 42388496"
    assert retain_message(text, AuditMessageMode.MASKED) == "mi número [number]"
    assert retain_message(text, AuditMessageMode.FULL) == text
    assert retain_message(text, AuditMessageMode.OMITTED) is None
    assert retain_message(None, AuditMessageMode.FULL) is None


# --- Cost --------------------------------------------------------------------------------------


def test_cost_counts_only_priced_calls() -> None:
    rates = TokenRates(input_usd_per_mtok=Decimal("2"), output_usd_per_mtok=Decimal("8"))
    # 1340 * 2 / 1e6 + 120 * 8 / 1e6 = 0.00268 + 0.00096; Kev adds nothing.
    assert estimate_cost(model_calls(), rates) == Decimal("0.003640")


def test_cached_input_has_its_own_rate_when_reported() -> None:
    # gpt-6-luna rates: 0.10 input, 0.01 cached input, 0.50 output (USD per million tokens).
    rates = TokenRates(Decimal("0.10"), Decimal("0.50"), Decimal("0.01"))
    call = model_calls()[0].model_copy(
        update={"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 0}
    )
    assert estimate_cost([call], rates) == Decimal("0.064000")  # 0.6 * 0.10 + 0.4 * 0.01


def test_cached_input_without_its_rate_or_report_is_charged_as_input() -> None:
    call = model_calls()[0].model_copy(
        update={"input_tokens": 1_000_000, "cached_input_tokens": 400_000, "output_tokens": 0}
    )
    assert estimate_cost([call], TokenRates(Decimal("0.10"), Decimal("0.50"))) == Decimal("0.1")
    unreported = call.model_copy(update={"cached_input_tokens": None})
    rates = TokenRates(Decimal("0.10"), Decimal("0.50"), Decimal("0.01"))
    assert estimate_cost([unreported], rates) == Decimal("0.1")


def test_rates_from_settings() -> None:
    from datetime import date

    from app.settings import Settings

    base = {"_env_file": None, "business_date": date(2026, 6, 17)}
    assert rates_from_settings(Settings(**base)) is None  # type: ignore[arg-type]
    configured = Settings(
        **base,  # type: ignore[arg-type]
        llm_input_usd_per_mtok=Decimal("0.10"),
        llm_output_usd_per_mtok=Decimal("0.50"),
        llm_cached_input_usd_per_mtok=Decimal("0.01"),
    )
    assert rates_from_settings(configured) == TokenRates(
        Decimal("0.10"), Decimal("0.50"), Decimal("0.01")
    )


def test_cost_is_unknown_without_rates() -> None:
    assert estimate_cost(model_calls(), None) is None


def test_call_without_token_counts_adds_nothing() -> None:
    call = model_calls()[0].model_copy(update={"input_tokens": None, "output_tokens": None})
    rates = TokenRates(input_usd_per_mtok=Decimal("2"), output_usd_per_mtok=Decimal("8"))
    assert estimate_cost([call], rates) == Decimal("0")


# --- Percentiles --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("values", "q", "expected"),
    [
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5, 5.5),
        ([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95, 9.55),
        ([10, 1, 7, 3], 0.5, 5.0),  # order does not matter
        ([42], 0.95, 42),
        ([1, 2], 0.0, 1),
        ([1, 2], 1.0, 2),
    ],
)
def test_percentile_on_known_sets(values: list[float], q: float, expected: float) -> None:
    assert percentile(values, q) == pytest.approx(expected)


def test_percentile_of_nothing_and_invalid_q() -> None:
    assert percentile([], 0.5) is None
    with pytest.raises(ValueError, match="between 0 and 1"):
        percentile([1], 1.5)


# --- Metrics -------------------------------------------------------------------------------------


def test_metrics_over_conversations() -> None:
    escalated = evaluate(request(flags={"human_requested": True}))
    traces = [
        # CONV-1: a clarification, then a verified automatic resolution (T1, es).
        trace(
            "T1",
            conversation_id="CONV-1",
            turn_index=0,
            outcome=Outcome.CLARIFY,
            tool_calls=[],
            total_latency_ms=1000.0,
            estimated_cost_usd=Decimal("0.002"),
        ),
        trace("T2", conversation_id="CONV-1", turn_index=1, total_latency_ms=3000.0),
        # CONV-2: escalated in Portuguese, no cost known.
        trace(
            "T3",
            conversation_id="CONV-2",
            language=Language.PT,
            outcome=Outcome.ESCALATE,
            decisions=[escalated],
            tool_calls=[],
            total_latency_ms=2000.0,
            estimated_cost_usd=None,
        ),
        # CONV-3: no decision (language could not be detected).
        trace(
            "T4",
            conversation_id="CONV-3",
            language=None,
            outcome=Outcome.CLARIFY,
            decisions=[],
            tool_calls=[],
            total_latency_ms=None,
            stage_latencies_ms={},
            estimated_cost_usd=Decimal("0.001"),
        ),
    ]
    metrics = compute_metrics(traces)
    assert metrics.turns == 4 and metrics.conversations == 3
    assert metrics.outcomes == {"RESOLVE": 1, "ESCALATE": 1, "CLARIFY": 1}
    assert metrics.turn_latency.count == 3
    assert metrics.turn_latency.p50_ms == 2000.0
    assert metrics.turn_latency.p95_ms == pytest.approx(2900.0)
    assert metrics.stage_latency["extract"].count == 3
    assert metrics.attempted_cases == 2
    assert metrics.automated_resolutions == 1
    assert metrics.total_cost_usd == Decimal("0.007")
    assert metrics.turns_without_cost == 1
    assert metrics.cost_per_attempted_case_usd == Decimal("0.003500")
    assert metrics.cost_per_automated_resolution_usd == Decimal("0.007000")
    assert metrics.outcomes_by_language == {
        "es": {"RESOLVE": 1},
        "pt": {"ESCALATE": 1},
        "unknown": {"CLARIFY": 1},
    }
    assert metrics.outcomes_by_tier == {
        "T1": {"RESOLVE": 1, "ESCALATE": 1},
        "unknown": {"CLARIFY": 1},
    }


def test_metrics_without_traces_or_costs() -> None:
    empty = compute_metrics([])
    assert empty.turns == 0 and empty.outcomes == {}
    assert empty.turn_latency.p50_ms is None
    assert empty.total_cost_usd is None and empty.cost_per_attempted_case_usd is None
    unresolved = compute_metrics([trace(outcome=None, tool_calls=[])])
    assert unresolved.outcomes == {"unknown": 1}
    assert unresolved.cost_per_automated_resolution_usd is None


def test_audit_masking_reuses_the_llm_minimization() -> None:
    from app.llm_adapter.minimization import scrub_message

    # Everything the LLM Adapter removes is removed from the log too (one implementation).
    for text in ("fecha de nacimiento 01/02/1990", "CPF 123.456.789-09", "cel 300 000 0000"):
        assert mask_message(text) == scrub_message(text)
