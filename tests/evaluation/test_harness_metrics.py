"""M18 metrics on known results: every number checked by hand."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.audit.cost import TokenRates
from app.contracts import ModelSignals, ModelSource, Outcome, ReasonCode
from app.evaluation.harness.estimate import estimate
from app.evaluation.harness.metrics import (
    CaseTruth,
    HardRuleFalseNegativeError,
    Prediction,
    assert_no_hard_rule_false_negatives,
    breakdown,
    percentile,
    score,
    summarize,
    variability,
)
from app.evaluation.harness.report import (
    BASELINE,
    SYSTEM,
    Arm,
    Evaluation,
    dashboard_json,
    evaluation_markdown,
    failures,
    results_csv,
    write_json,
)
from app.evaluation.harness.signals import SignalTurn, brier, compare, ece
from app.evaluation.labeler import CaseLabel, DisputeLabel
from app.evaluation.scenarios import Path3


def label(
    outcome: str,
    rules: tuple[str, ...] = (),
    queue: str | None = None,
    priority: str | None = None,
    actions: tuple[str, ...] = (),
    transaction: str | None = "t1",
) -> CaseLabel:
    dispute = DisputeLabel(
        transaction=transaction,
        outcome=Outcome(outcome),
        triggered_rules=list(rules),
        failed_gates=[],
        queue=queue,
        priority=priority,
        inform_reason=None,
        clarify_target=None,
        tier="T1",
        reason_code=None,
        actions=list(actions),
    )
    return CaseLabel(
        case_id="S001",
        language="es",
        path=Path3.RESOLUTION,
        outcome=Outcome(outcome),
        triggered_rules=list(rules),
        disputes=[dispute],
    )


RESOLVE = CaseTruth(
    "S001", label("RESOLVE", actions=("ACT-02", "ACT-04")), "es", "seeded", "Colombia"
)
FRAUD = CaseTruth(
    "S071",
    label("ESCALATE", ("ESC-03",), "fraud", "high", ("ACT-03", "ACT-05")),
    "pt",
    "seeded",
    "México",
)
INJECTION = CaseTruth(
    "S092",
    label("ESCALATE", ("ESC-13",), "security_review", "normal", ("ACT-05",)),
    "es",
    "real",
    "Argentina",
)
INFORM = CaseTruth("R010", label("INFORM"), "pt", "real", "Colombia")


def test_a_correct_resolution() -> None:
    p = Prediction(
        "S001", "r1", "RESOLVE", actions=("ACT-02", "ACT-04"), handed_off=False, end="final"
    )
    s = score(RESOLVE, p)
    assert s.full_ok and not s.unsafe and s.auto_resolved_ok and s.contained and s.fcr


def test_a_resolution_that_should_have_escalated_is_unsafe_and_a_false_negative() -> None:
    p = Prediction("S071", "r1", "RESOLVE", actions=("ACT-02", "ACT-04"), handed_off=False)
    s = score(FRAUD, p)
    assert s.unsafe == ("resolve_not_labeled", "unauthorized_ACT-02", "unauthorized_ACT-04")
    assert s.hard_false_negatives == ("ESC-03",) and s.missing_escalation
    assert not s.full_ok and s.fcr is None
    with pytest.raises(HardRuleFalseNegativeError, match="r1:S071:ESC-03"):
        assert_no_hard_rule_false_negatives([s])


def test_the_wrong_rule_is_a_false_negative_too() -> None:
    p = Prediction("S071", "r1", "ESCALATE", ("ESC-05",), "disputes", "normal", (), True)
    s = score(FRAUD, p)
    assert s.outcome_ok and not s.rules_ok and s.hard_false_negatives == ("ESC-03",)


def test_interruptions_do_not_compare_the_priority() -> None:
    p = Prediction("S092", "r1", "ESCALATE", ("ESC-13",), "security_review", "high", (), True)
    s = score(INJECTION, p)
    assert s.priority_ok is None and s.full_ok
    assert INJECTION.interruption and not FRAUD.interruption


def test_an_unnecessary_escalation() -> None:
    p = Prediction("R010", "r1", "ESCALATE", ("ESC-09",), "disputes", "normal", ("ACT-05",), True)
    s = score(INFORM, p)
    assert s.unnecessary_escalation and not s.unsafe and s.contained is False and s.fcr is False


def test_the_baseline_has_no_actions_and_no_containment() -> None:
    p = Prediction("S001", "baseline", "RESOLVE")
    s = score(RESOLVE, p)
    assert s.full_ok and s.contained is None and s.auto_resolved and s.fcr


def test_percentiles() -> None:
    assert percentile([], 0.5) is None
    assert percentile([1.0, 2.0, 3.0, 4.0], 0.5) == 2.5
    assert percentile([10.0], 0.95) == 10.0
    assert percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.95) == pytest.approx(4.8)


def test_the_summary() -> None:
    preds = [
        Prediction(
            "S001",
            "r1",
            "RESOLVE",
            actions=("ACT-02", "ACT-04"),
            handed_off=False,
            turn_ms=(1000.0, 3000.0),
            conversation_ms=4000.0,
            cost_usd=Decimal("0.02"),
            end="final",
        ),
        Prediction(
            "S071",
            "r1",
            "RESOLVE",
            actions=("ACT-02",),
            handed_off=False,
            turn_ms=(2000.0,),
            conversation_ms=2000.0,
            cost_usd=Decimal("0.01"),
            end="final",
        ),
        Prediction(
            "R010",
            "r1",
            "INFORM",
            handed_off=False,
            turn_ms=(500.0,),
            conversation_ms=500.0,
            cost_usd=Decimal("0.01"),
            end="max_turns",
            out_of_script=2,
        ),
    ]
    truths = {"S001": RESOLVE, "S071": FRAUD, "R010": INFORM}
    scores = [score(truths[p.key], p) for p in preds]
    out = summarize(scores, preds)
    assert out["cases"] == 3 and out["outcome_accuracy"] == pytest.approx(2 / 3)
    assert out["unsafe"] == 1 and out["hard_rule_false_negatives"] == 1
    assert out["hard_rule_false_negative_cases"] == ["S071:ESC-03"]
    assert out["missing_escalations"] == 1 and out["containment"] == 1.0
    assert out["automatic_resolution"] == pytest.approx(2 / 3)
    assert out["successful_automatic_resolution"] == pytest.approx(1 / 3)
    assert out["fcr"] == 0.5  # S001 yes; R010 reached the turn limit
    assert out["turn_latency_ms_p50"] == 1500.0 and out["conversation_latency_ms_p50"] == 2000.0
    assert out["cost_usd_total"] == pytest.approx(0.04) and out[
        "cost_usd_per_case"
    ] == pytest.approx(0.04 / 3)
    assert out["cost_usd_per_successful_resolution"] == pytest.approx(0.04)
    assert out["out_of_script_conversations"] == 1 and out["max_turns"] == 1
    unknown = summarize(scores, [preds[0], Prediction("S071", "r1", "RESOLVE")])
    assert unknown["cost_usd_total"] is None  # a cost without rates is unknown, never zero


def test_the_breakdown_and_variability() -> None:
    truths = {"S001": RESOLVE, "R010": INFORM}
    runs = [
        Prediction("S001", "r1", "RESOLVE", actions=("ACT-02",), handed_off=False),
        Prediction("S001", "r2", "INFORM", handed_off=False),
        Prediction("R010", "r1", "INFORM", handed_off=False),
        Prediction("R010", "r2", "INFORM", handed_off=False),
    ]
    scores = [score(truths[p.key], p) for p in runs]
    groups = breakdown(truths, scores)
    assert groups["data_source"]["real"]["outcome_accuracy"] == 1.0
    assert groups["data_source"]["seeded"]["outcome_accuracy"] == 0.5
    assert groups["country"]["Colombia"]["cases"] == 4
    spread = variability(runs, scores)
    assert spread.changed == ("S001",) and spread.change_rate == 0.5
    assert spread.full_match_by_run == (1.0, 0.5)


# --- Kev against the fallback ---------------------------------------------------------------


def test_brier_and_ece() -> None:
    assert brier([]) is None and ece([]) is None
    assert brier([(1.0, 1), (0.0, 0)]) == 0.0
    assert brier([(0.8, 1), (0.6, 0)]) == pytest.approx((0.04 + 0.36) / 2)
    assert ece([(0.9, 1), (0.9, 0)]) == pytest.approx(0.4)  # one bin: |0.9 - 0.5|
    assert ece([(1.0, 1)]) == 0.0


def test_the_learned_component_comparison() -> None:
    kev = ModelSignals(
        source=ModelSource.KEV,
        reason_code_probs={ReasonCode.UNRECOGNIZED: 0.6},
        reason_code_other=0.4,
        ambiguity=0.7,
        escalation_risk=0.2,
    )
    fallback = ModelSignals(
        source=ModelSource.LLM_FALLBACK,
        reason_code_probs={ReasonCode.DUPLICATE: 1.0},
        ambiguity=0.0,
        escalation_risk=0.0,
    )
    turns = [
        SignalTurn("kev", kev, ReasonCode.UNRECOGNIZED, 1, 0),
        SignalTurn("llm_fallback", fallback, ReasonCode.UNRECOGNIZED, 1, 0),
        SignalTurn("llm_fallback", ModelSignals(source=ModelSource.LLM_FALLBACK), None, 0, 1),
    ]
    out = compare(turns)
    k, f = out["kev"], out["llm_fallback"]
    assert k["reason_code"]["accuracy"] == 1.0 and k["reason_code"]["coverage"] == 1.0  # type: ignore[index]
    assert k["reason_code"]["brier"] == pytest.approx(0.16 + 0.16)  # type: ignore[index]
    assert f["reason_code"]["accuracy"] == 0.0 and f["turns"] == 2  # type: ignore[index]
    assert k["ambiguous"]["brier"] == pytest.approx(0.09)  # type: ignore[index]
    assert f["escalation_risk"]["turns"] == 1  # type: ignore[index]  # the empty one has none
    assert compare([])["kev"] is None


# --- Estimate and outputs ---------------------------------------------------------------------


def test_the_estimate() -> None:
    rates = TokenRates(input_usd_per_mtok=Decimal("1"), output_usd_per_mtok=Decimal("2"))
    out = estimate(10, 3, True, True, 5, 40_000, rates)
    assert out.conversations == 40 and out.turns == 200.0
    assert out.baseline_input_tokens == 10 * (10_000 + 1000)
    assert out.cost_usd is not None and out.cost_usd > 0
    assert estimate(10, 1, False, False, 5, 0, None).cost_usd is None
    assert any("unknown" in line for line in estimate(1, 1, False, False, 1, 0, None).lines())


def evaluation() -> Evaluation:
    truths = {"S001": RESOLVE, "S071": FRAUD}
    system, base = Arm(SYSTEM), Arm(BASELINE)
    for run in ("system-1", "system-2"):
        for p in (
            Prediction(
                "S001",
                run,
                "RESOLVE",
                actions=("ACT-02", "ACT-04"),
                handed_off=False,
                turn_ms=(1000.0,),
                conversation_ms=1000.0,
                end="final",
            ),
            Prediction(
                "S071",
                run,
                "ESCALATE",
                ("ESC-03",),
                "fraud",
                "high",
                ("ACT-05",),
                True,
                (900.0,),
                900.0,
                end="handoff",
            ),
        ):
            system.predictions.append(p)
            system.scores.append(score(truths[p.key], p))
    for p in (Prediction("S001", "baseline", "RESOLVE"), Prediction("S071", "baseline", "INFORM")):
        base.predictions.append(p)
        base.scores.append(score(truths[p.key], p))
    base.rationales = {"S071": "a lost phone is not a dispute"}
    now = datetime(2026, 10, 3, 12, tzinfo=UTC)
    return Evaluation(
        now,
        now,
        {"policy": "0.4.11"},
        {"cases": 2},
        truths,
        {"S001": "unrecognized", "S071": "takeover"},
        {"S001": "resolution", "S071": "human"},
        {SYSTEM: system, BASELINE: base},
        compare([]),
    )


def test_the_outputs_name_cases_by_key_only() -> None:
    e = evaluation()
    text = results_csv(e)
    assert text.splitlines()[0].startswith("arm,run,case_key")
    assert len(text.splitlines()) == 1 + 4 + 2
    data = dashboard_json(e)
    assert data["arms"][SYSTEM]["pooled"]["full_match"] == 1.0
    assert data["arms"][BASELINE]["pooled"]["hard_rule_false_negatives"] == 1
    assert data["arms"][SYSTEM]["variability"]["change_rate"] == 0.0
    failed = failures(e)
    assert [(f["arm"], f["case_key"]) for f in failed] == [(BASELINE, "S071")]
    assert failed[0]["rationale"] == "a lost phone is not a dispute"
    doc = evaluation_markdown(e)
    assert "| Full match | 100.0% | 50.0% |" in doc and "S071" in doc
    assert "CLI-" not in doc + text + write_json(data)


def test_a_partial_run_is_marked() -> None:
    e = evaluation()
    e.partial = True
    assert "Partial run" in evaluation_markdown(e)


def test_too_many_turns_out_of_script_is_invalid_never_correct() -> None:
    p = Prediction(
        "S001",
        "r1",
        "RESOLVE",
        actions=("ACT-02", "ACT-04"),
        handed_off=False,
        end="final",
        out_of_script=4,
    )
    s = score(RESOLVE, p)
    assert s.invalid and not s.outcome_ok and not s.full_ok and s.fcr is False
    assert not score(
        RESOLVE,
        Prediction("S001", "r1", "RESOLVE", actions=("ACT-02",), handed_off=False, out_of_script=3),
    ).invalid
    assert summarize([s], [p])["invalid_conversations"] == 1


def test_the_report_of_a_second_run() -> None:
    e = evaluation()
    e.previous = {"arms": {}}
    e.previous_report = "# Evaluation\n\n## 5. Results\n\nrun one numbers"
    e.review = {"reviewed": 50, "confirmed": 50, "discrepancies": []}
    doc = evaluation_markdown(e)
    summary = doc.split("## Executive summary")[1].split("## 1. Setup")[0]
    assert summary.count("\n- ") == 5 and "Run 2, not blind" in summary
    run1 = doc.split("## 12. Run 1 (before evaluation-driven fixes)")[1]
    assert "### 5. Results" in run1 and "run one numbers" in run1  # kept, one level down
    assert "50 of 50 labels confirmed, 0 discrepancies" in doc
    assert "| country | Colombia | 1 |" in doc  # the composition behind the fairness breakdown
    assert "Not publishable" not in doc


def test_baseline_errors_make_the_report_not_publishable() -> None:
    e = evaluation()
    e.arms[BASELINE].predictions.append(Prediction("S001", "baseline-2", None, error="429"))
    assert "Not publishable" in evaluation_markdown(e)
    assert "First measurement" in evaluation_markdown(e)


def test_the_report_notes_a_dirty_tree_latency_and_analyses() -> None:
    e = evaluation()
    e.versions = {"git_commit": "f655707", "git_dirty": True}
    e.analyses = {"baseline:S071": "the fraud queue was invisible"}
    e.arms["system_no_connect"] = e.arms[SYSTEM]
    doc = evaluation_markdown(e)
    assert "uncommitted changes on top of commit f655707" in doc
    assert "the fraud queue was invisible" in doc and "to analyze" not in doc.split("## 12.")[0]
    assert "Connecting sentences do not change accuracy" in doc
    assert "deterministic backup" in doc
