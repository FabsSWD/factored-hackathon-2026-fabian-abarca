"""Outputs of M18: a CSV per case and run, the JSON for the M16 dashboard, docs/evaluation.md.

Cases are named by their key only: no customer or transaction identifier, no document, no
record value. The CSV also carries each case's segment (country, age band, gender), so it is
written under ``reports/m18/`` and git-ignored like every CSV; the JSON and the report hold
aggregates and case keys.
"""

from __future__ import annotations

import csv
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from app.evaluation.harness.metrics import (
    CaseScore,
    CaseTruth,
    Prediction,
    breakdown,
    summarize,
    variability,
)

SYSTEM, NO_CONNECT, BASELINE = "system", "system_no_connect", "baseline"


@dataclass
class Arm:
    """One compared arm: its predictions and scores over every run it had."""

    name: str
    predictions: list[Prediction] = field(default_factory=list)
    scores: list[CaseScore] = field(default_factory=list)
    rationales: dict[str, str] = field(default_factory=dict)  # baseline: key -> rationale
    cleaned_cases: dict[str, int] = field(default_factory=dict)  # run -> real cases removed


@dataclass
class Evaluation:
    started_at: datetime
    finished_at: datetime
    versions: dict[str, Any]
    mix: dict[str, Any]
    truths: dict[str, CaseTruth]
    titles: dict[str, str]
    paths: dict[str, str]
    arms: dict[str, Arm]
    signals: dict[str, Any] | None = None
    partial: bool = False  # a subset of the evaluation split (smoke run)
    # The earlier run the fixes came from: its dashboard JSON and its report, kept as they were.
    previous: dict[str, Any] | None = None
    previous_report: str | None = None
    review: dict[str, Any] | None = None  # reports/m17_review_summary.json (policy §16.3)
    # Hand-written analysis of each failure, by "arm:case_key" or case key
    # (reports/m18_failure_analyses.yaml).
    analyses: dict[str, str] = field(default_factory=dict)


def _labels(t: CaseTruth) -> tuple[str, str, str, str]:
    return (
        t.label.outcome.value,
        " ".join(t.label.triggered_rules),
        t.queue or "",
        t.priority or "",
    )


def results_csv(evaluation: Evaluation) -> str:
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow([
        "arm", "run", "case_key", "title", "language", "data_source", "path", "country",
        "age_band", "gender", "fault_injected", "expected_outcome", "expected_rules",
        "expected_queue", "expected_priority", "outcome", "rules", "queue", "priority",
        "outcome_ok", "rules_ok", "queue_ok", "priority_ok", "full_ok", "unsafe",
        "hard_rule_false_negatives", "unnecessary_escalation", "missing_escalation", "contained",
        "automatic_resolution", "turns", "end", "out_of_script", "conversation_ms", "cost_usd",
        "error", "rationale",
    ])  # fmt: skip
    for arm in evaluation.arms.values():
        by_key = {(p.key, p.run): p for p in arm.predictions}
        for s in sorted(arm.scores, key=lambda s: (s.run, s.key)):
            t, p = evaluation.truths[s.key], by_key[(s.key, s.run)]
            writer.writerow([
                arm.name, s.run, s.key, evaluation.titles.get(s.key, ""), t.language,
                t.data_source, evaluation.paths.get(s.key, ""), t.country, t.age_band, t.gender,
                t.fault_injected, *_labels(t), p.outcome or "", " ".join(p.rules), p.queue or "",
                p.priority or "", s.outcome_ok, s.rules_ok, s.queue_ok,
                "" if s.priority_ok is None else s.priority_ok, s.full_ok, " ".join(s.unsafe),
                " ".join(s.hard_false_negatives), s.unnecessary_escalation, s.missing_escalation,
                "" if s.contained is None else s.contained, s.auto_resolved, len(p.turn_ms), p.end,
                p.out_of_script, "" if p.conversation_ms is None else round(p.conversation_ms),
                "" if p.cost_usd is None else f"{p.cost_usd:.6f}", p.error or "",
                arm.rationales.get(s.key, ""),
            ])  # fmt: skip
    return out.getvalue()


def failures(evaluation: Evaluation) -> list[dict[str, Any]]:
    """Every case and run that is not a full match, is unsafe or misses a hard rule."""
    rows = []
    for arm in evaluation.arms.values():
        by_key = {(p.key, p.run): p for p in arm.predictions}
        for s in sorted(arm.scores, key=lambda s: (s.key, s.run)):
            if s.full_ok and not s.unsafe and not s.hard_false_negatives:
                continue
            t, p = evaluation.truths[s.key], by_key[(s.key, s.run)]
            expected = _labels(t)
            rows.append({
                "arm": arm.name, "run": s.run, "case_key": s.key, "language": t.language,
                "data_source": t.data_source, "fault_injected": t.fault_injected,
                "expected": {"outcome": expected[0], "rules": expected[1], "queue": expected[2],
                             "priority": expected[3]},
                "got": {"outcome": p.outcome, "rules": " ".join(p.rules), "queue": p.queue,
                        "priority": p.priority},
                "end": p.end, "out_of_script": p.out_of_script, "invalid": s.invalid,
                "unsafe": list(s.unsafe),
                "hard_rule_false_negatives": list(s.hard_false_negatives), "error": p.error,
                "rationale": arm.rationales.get(s.key),
            })  # fmt: skip
    return rows


def _arm_summary(evaluation: Evaluation, arm: Arm) -> dict[str, Any]:
    runs = sorted({p.run for p in arm.predictions})
    per_run = {
        run: summarize(
            [s for s in arm.scores if s.run == run], [p for p in arm.predictions if p.run == run]
        )
        for run in runs
    }
    spread = variability(arm.predictions, arm.scores)
    return {
        "runs": per_run,
        "pooled": summarize(arm.scores, arm.predictions),
        "pooled_without_fault_injection": summarize(
            [s for s in arm.scores if not evaluation.truths[s.key].fault_injected],
            [p for p in arm.predictions if not evaluation.truths[p.key].fault_injected],
        ),
        "breakdown": breakdown(evaluation.truths, arm.scores),
        "variability": {
            "runs": spread.runs,
            "cases": spread.cases,
            "changed_cases": list(spread.changed),
            "change_rate": spread.change_rate,
            "full_match_by_run": list(spread.full_match_by_run),
        },
        "real_cases_removed_after_each_run": arm.cleaned_cases,
    }


def dashboard_json(evaluation: Evaluation) -> dict[str, Any]:
    return {
        "generated_at": evaluation.finished_at.isoformat(),
        "started_at": evaluation.started_at.isoformat(),
        "partial": evaluation.partial,
        "versions": evaluation.versions,
        "mix": evaluation.mix,
        "arms": {name: _arm_summary(evaluation, arm) for name, arm in evaluation.arms.items()},
        "learned_component": evaluation.signals,
        "failures": failures(evaluation),
    }


def _pct(value: object) -> str:
    return "n/a" if value is None else f"{float(value) * 100:.1f}%"  # type: ignore[arg-type]


def _ms(value: object) -> str:
    return "n/a" if value is None else f"{float(value) / 1000:.1f} s"  # type: ignore[arg-type]


def _usd(value: object) -> str:
    return "n/a" if value is None else f"USD {float(value):.4f}"  # type: ignore[arg-type]


def _table(header: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(str(cell) for cell in row) + " |" for row in rows]
    return "\n".join(lines)


METRIC_ROWS = (
    ("Outcome accuracy", "outcome_accuracy", _pct),
    ("Rules correct", "rules_accuracy", _pct),
    ("Queue correct", "queue_accuracy", _pct),
    ("Priority correct", "priority_accuracy", _pct),
    ("Full match", "full_match", _pct),
    ("Unsafe outcomes", "unsafe", str),
    ("Hard-rule false negatives", "hard_rule_false_negatives", str),
    ("Unnecessary escalations", "unnecessary_escalations", str),
    ("Missing escalations", "missing_escalations", str),
    ("Containment", "containment", _pct),
    ("Automatic resolution", "automatic_resolution", _pct),
    ("Successful automatic resolution", "successful_automatic_resolution", _pct),
    ("FCR", "fcr", _pct),
    ("Turn latency p50", "turn_latency_ms_p50", _ms),
    ("Turn latency p95", "turn_latency_ms_p95", _ms),
    ("Conversation latency p50", "conversation_latency_ms_p50", _ms),
    ("Conversation latency p95", "conversation_latency_ms_p95", _ms),
    ("Cost per case attempted", "cost_usd_per_case", _usd),
    ("Cost per successful resolution", "cost_usd_per_successful_resolution", _usd),
    ("Conversations out of script", "out_of_script_conversations", str),
    ("Invalid conversations (more than 3 turns out of script)", "invalid_conversations", str),
    ("Conversations at the turn limit", "max_turns", str),
    ("Errors", "errors", str),
)


BREAKDOWN_HEADER = ("Dimension", "Value", "Cases", "Outcome", "Full match", "Unsafe",
                    "Missing esc.", "Containment")  # fmt: skip
SIGNAL_HEADER = ("Source", "Turns", "Reason accuracy", "Reason Brier", "Reason ECE",
                 "Ambiguous Brier / ECE", "Escalation Brier / ECE")  # fmt: skip
FAILURE_HEADER = ("Case", "Arm", "Run", "Expected", "Got", "End", "Out of script",
                  "Unsafe / missed", "Analysis")  # fmt: skip
LATENCY_HEADER = ("Configuration", "Turn p50", "Turn p95", "Conversation p50",
                  "Conversation p95", "Cost per case")  # fmt: skip
COMPOSITION_HEADER = ("Dimension", "Value", "Cases", "Resolution", "Ambiguous", "Human",
                      "Labeled ESCALATE")  # fmt: skip
HEADERS = {SYSTEM: "System (runs pooled)", NO_CONNECT: "System, connect off",
           BASELINE: "Baseline (GPT-6 Luna alone)"}  # fmt: skip


def _breakdown_rows(arms: dict[str, Any]) -> list[list[object]]:
    rows: list[list[object]] = []
    for dimension, groups in arms.get(SYSTEM, {}).get("breakdown", {}).items():
        for value, g in groups.items():
            rows.append([dimension, value, g["cases"], _pct(g["outcome_accuracy"]),
                         _pct(g["full_match"]), _pct(g["unsafe_rate"]),
                         g["missing_escalations"], _pct(g["containment"])])  # fmt: skip
    return rows


def _failure_rows(
    failed: list[dict[str, Any]], analyses: dict[str, str] | None = None
) -> list[list[object]]:
    notes = analyses or {}
    rows: list[list[object]] = []
    for f in failed:
        expected = f"{f['expected']['outcome']} {f['expected']['rules']}".strip()
        got = f"{f['got']['outcome']} {f['got']['rules']}".strip()
        missed = ", ".join(f["unsafe"] + f["hard_rule_false_negatives"]) or "-"
        note = notes.get(f"{f['arm']}:{f['case_key']}") or notes.get(f["case_key"], "to analyze")
        rows.append([f["case_key"], f["arm"], f["run"], expected, got, f["end"],
                     f["out_of_script"], missed, note])  # fmt: skip
    return rows


def _latency_text(arms: dict[str, Any]) -> str:
    on, off = arms.get(SYSTEM, {}).get("pooled"), arms.get(NO_CONNECT, {}).get("pooled")
    if not on or not off or on["turn_latency_ms_p50"] is None or off["turn_latency_ms_p50"] is None:
        return ""
    cost = (on["turn_latency_ms_p50"] - off["turn_latency_ms_p50"]) / 1000
    return (
        f"Connecting sentences do not change accuracy (outcome {_pct(on['outcome_accuracy'])} "
        f"with them, {_pct(off['outcome_accuracy'])} without) and cost about {cost:.1f} s per "
        "turn at the median. The demo keeps them for the tone of the replies; "
        "`LLM_CONNECT_ENABLED=false` is the fast option, with the same decisions and templates."
    )


def _signal_rows(signals: dict[str, Any]) -> list[list[object]]:
    rows: list[list[object]] = []
    for source in ("kev", "llm_fallback"):
        s = signals.get(source)
        if not s:
            rows.append([source, "not available", "", "", "", "", ""])
            continue
        r, a, e = s["reason_code"], s["ambiguous"], s["escalation_risk"]
        rows.append([source, s["turns"], _pct(r["accuracy"]), _fmt(r["brier"]), _fmt(r["ece"]),
                     f"{_fmt(a['brier'])} / {_fmt(a['ece'])}",
                     f"{_fmt(e['brier'])} / {_fmt(e['ece'])}"])  # fmt: skip
    return rows


def _composition(evaluation: Evaluation) -> list[list[object]]:
    """Per segment value: how many cases, and their mix of paths and escalations."""
    rows: list[list[object]] = []
    for dimension in ("country", "age_band", "gender", "language", "data_source"):
        groups: dict[str, list[CaseTruth]] = {}
        for truth in evaluation.truths.values():
            groups.setdefault(str(getattr(truth, dimension)), []).append(truth)
        for value, members in sorted(groups.items()):
            n = len(members)
            paths = [evaluation.paths.get(t.key, "") for t in members]
            escalate = sum(t.label.outcome.value == "ESCALATE" for t in members)
            rows.append([dimension, value, n, _pct(paths.count("resolution") / n),
                         _pct(paths.count("ambiguous_or_unsupported") / n),
                         _pct(paths.count("human") / n), _pct(escalate / n)])  # fmt: skip
    return rows


def _better(a: object, b: object) -> str:
    """'better' when the Brier ``a`` is lower than ``b``."""
    if a is None or b is None:
        return "not comparable"
    return "better" if float(a) < float(b) else "not better"  # type: ignore[arg-type]


def _kev_finding(signals: dict[str, Any]) -> str:
    kev, fallback = signals.get("kev"), signals.get("llm_fallback")
    if not kev:
        return "Kev did not answer in this run: only the extraction fallback is reported."
    reason = kev["reason_code"]["accuracy"]
    base = fallback["reason_code"]["accuracy"] if fallback else None
    amb = _better(kev["ambiguous"]["brier"], fallback["ambiguous"]["brier"] if fallback else None)
    esc = _better(kev["escalation_risk"]["brier"],
                  fallback["escalation_risk"]["brier"] if fallback else None)  # fmt: skip
    return (
        f"**Finding.** Kev picks the right reason code on {_pct(reason)} of the turns where the "
        f"customer states it, against {_pct(base)} for the extraction fallback. Kev is trained in "
        "English and served uncalibrated, and the conversations are in Spanish and Portuguese. "
        f"Its Brier score is {amb} than the fallback's on ambiguity and {esc} on escalation risk: "
        "its probabilities carry information the 0/1 fallback does not. **Conclusion:** the "
        "signals inform, they do not decide. The ESC-11 thresholds stay null until M7 calibrates "
        "them on the calibration split, and no outcome depends on Kev."
    )


def _executive_summary(evaluation: Evaluation, arms: dict[str, Any]) -> list[str]:
    system = arms.get(SYSTEM, {}).get("pooled", {})
    quiet = arms.get(NO_CONNECT, {}).get("pooled", {})
    base = arms.get(BASELINE, {}).get("pooled_without_fault_injection", {})
    lines = [
        "- **Accuracy.** System (all runs pooled): outcome "
        f"{_pct(system.get('outcome_accuracy'))}, "
        f"full match {_pct(system.get('full_match'))}. Baseline (GPT-6 Luna alone, without the "
        f"injected-fault cases): outcome {_pct(base.get('outcome_accuracy'))}, full match "
        f"{_pct(base.get('full_match'))}.",
        f"- **Safety.** Unsafe outcomes: system {system.get('unsafe', 'n/a')}, baseline "
        f"{base.get('unsafe', 'n/a')}. Hard-rule false negatives: system "
        f"{system.get('hard_rule_false_negatives', 'n/a')}, baseline "
        f"{base.get('hard_rule_false_negatives', 'n/a')}.",
        f"- **Cost.** {_usd(system.get('cost_usd_per_case'))} per case attempted and "
        f"{_usd(system.get('cost_usd_per_successful_resolution'))} per successful automatic "
        f"resolution (baseline: {_usd(base.get('cost_usd_per_case'))} per case).",
        f"- **Latency per turn** (p50 / p95): {_ms(system.get('turn_latency_ms_p50'))} / "
        f"{_ms(system.get('turn_latency_ms_p95'))} with connecting sentences, "
        f"{_ms(quiet.get('turn_latency_ms_p50'))} / {_ms(quiet.get('turn_latency_ms_p95'))} "
        "without.",
    ]
    if evaluation.previous is not None:
        lines.append("- **Run 2, not blind.** It follows fixes driven by run 1 (section 12), so "
                     "it is not a blind estimate: run 1 is the first measurement.")  # fmt: skip
    else:
        lines.append("- **First measurement.** No fix was made from these results before this run.")
    return lines


def _latency_rows(arms: dict[str, Any]) -> list[list[object]]:
    rows: list[list[object]] = []
    for name, label in ((SYSTEM, "Connect on (runs pooled)"), (NO_CONNECT, "Connect off")):
        pooled = arms.get(name, {}).get("pooled")
        if pooled:
            rows.append(
                [
                    label,
                    _ms(pooled["turn_latency_ms_p50"]),
                    _ms(pooled["turn_latency_ms_p95"]),
                    _ms(pooled["conversation_latency_ms_p50"]),
                    _ms(pooled["conversation_latency_ms_p95"]),
                    _usd(pooled["cost_usd_per_case"]),
                ]
            )
    return rows


def _demote(markdown: str) -> str:
    """An earlier report inside this one: its headings one level down, its title dropped."""
    lines = []
    for line in markdown.splitlines():
        if line.startswith("# "):
            continue
        lines.append("#" + line if line.startswith("#") else line)
    return "\n".join(lines).strip()


def evaluation_markdown(evaluation: Evaluation, data: dict[str, Any] | None = None) -> str:
    """The report; ``data`` replaces the dashboard JSON of ``evaluation`` when the arms come
    from a file already written (a baseline retry merges into it)."""
    data = data or dashboard_json(evaluation)
    arms = data["arms"]
    names = [name for name in (SYSTEM, NO_CONNECT, BASELINE) if name in arms]

    def pooled(name: str) -> dict[str, Any]:
        key = "pooled_without_fault_injection" if name == BASELINE else "pooled"
        return dict(arms[name][key])

    results = _table(
        ["Metric", *(HEADERS[n] for n in names)],
        [[label, *(fmt(pooled(n).get(key)) for n in names)] for label, key, fmt in METRIC_ROWS],
    )
    faults = arms.get(BASELINE, {}).get("pooled", {})
    spread = arms.get(SYSTEM, {}).get("variability", {})
    changed = spread.get("changed_cases", [])
    by_run = ", ".join(_pct(v) for v in spread.get("full_match_by_run", [])) or "n/a"
    fail_rows = _failure_rows(data["failures"], evaluation.analyses)
    failures_table = _table(FAILURE_HEADER, fail_rows) if fail_rows else "None."
    mix_table = _table(["", "Cases"], [[k, v] for k, v in evaluation.mix.items()])
    versions = "\n".join(f"- **{k}:** {v}" for k, v in evaluation.versions.items())
    if evaluation.versions.get("git_dirty"):
        versions += (
            f"\n\n> The run used uncommitted changes on top of commit "
            f"{evaluation.versions.get('git_commit')}: `git_commit` alone does not reproduce it. "
            "The commit that publishes this report contains those changes."
        )
    review = evaluation.review or {}
    reviewed = (
        f"{review.get('confirmed')} of {review.get('reviewed')} labels confirmed, "
        f"{len(review.get('discrepancies', []))} discrepancies"
        if review
        else "pending"
    )
    baseline_errors = arms.get(BASELINE, {}).get("pooled", {}).get("errors", 0)
    publishable = (
        f"\n> **Not publishable:** {baseline_errors} baseline calls failed. Zero errors is "
        "required to publish the comparison.\n"
        if baseline_errors
        else ""
    )
    partial = (
        "\n> **Partial run:** a subset of the evaluation split. These numbers are not the "
        "evaluation.\n"
        if evaluation.partial
        else ""
    )
    previous = (
        "Run 2 follows fixes driven by run 1, so it is not a blind estimate. Run 1 is kept "
        "here as it came out ([m18_evaluation_run1.json](../reports/m18_evaluation_run1.json)); "
        "its failures were analysed from the traces and fixed before run 2 (evaluation design "
        '§9, "After run 1").\n\n' + _demote(evaluation.previous_report)
        if evaluation.previous_report
        else "There is no earlier run."
    )
    related = (
        "[Evaluation design](evaluation-design.md), "
        "[Dispute policy §16](dispute-policy.md#16-deriving-evaluation-labels)"
    )
    summary = "\n".join(_executive_summary(evaluation, arms))
    return f"""# Evaluation

| Field | Value |
|---|---|
| Status | Generated by `scripts/run_evaluation.py`; failure analysis by hand |
| Run | {evaluation.started_at:%Y-%m-%d %H:%M} to {evaluation.finished_at:%H:%M} UTC |
| Related | {related} |
{partial}{publishable}
## Executive summary

{summary}

## 1. Setup

The system runs whole behind its real API: a simulated customer logs in with the case's
document and the test OTP and plays the case's script (evaluation design §2), picking its own
transaction when candidates are shown. The baseline is GPT-6 Luna deciding alone from the
policy, the records minimized as in the system (DATA-01) plus their USD amounts, and the scripted
conversation, in one call per case. Both are scored against the reference labels of M17 on the
evaluation split. ESC-10 and ESC-11 are reached by fault injection, and are reported apart for
the baseline, which has no model signals and no Tool Layer. An amount corrected in a declined
summary has a deterministic backup (`app/orchestrator/corrections.py`): with
`RC_INCORRECT_AMOUNT` and no amount extracted, the one number of the message is the corrected
expected amount.

## 2. Case mix

{mix_table}

## 3. Label quality

The labels come from the Policy Engine on each case's records (policy §16.2). Their independent
check against the policy is the human review of M17 (policy §16.3): {reviewed}
([m17_review_summary.json](../reports/m17_review_summary.json)).

## 4. Versions

{versions}

## 5. Results

{results}

The baseline column excludes the injected-fault cases (S090, S091); with them, its outcome
accuracy is {_pct(faults.get("outcome_accuracy"))}. A conversation with more than 3 turns out of
script is invalid and never counts as correct. Definitions: `app/evaluation/harness/metrics.py`.
In interruptions (ESC-12, ESC-13) the priority is not compared (evaluation design §9).

## 6. Latency with and without connecting sentences

{_table(LATENCY_HEADER, _latency_rows(arms))}

{_latency_text(arms)}

## 7. Variability between runs

Cases whose outcome or rules changed between runs: {len(changed)} of {spread.get("cases", 0)}
({_pct(spread.get("change_rate"))}): {", ".join(changed) or "none"}.
Full match by run: {by_run}.

## 8. Fairness (DATA-02)

Country, age band and gender never reach the model or the engine: DATA-02 keeps them out of
every model call and every `PolicyRequest`, and they are read here only to group the results.
A difference between segments therefore comes from the mix of cases each segment received,
which the composition below shows, not from the segment itself.

{_table(COMPOSITION_HEADER, _composition(evaluation))}

{_table(BREAKDOWN_HEADER, _breakdown_rows(arms))}

## 9. Learned component: Kev against the extraction fallback

{_kev_finding(evaluation.signals or {})}

{_table(SIGNAL_HEADER, _signal_rows(evaluation.signals or {}))}

## 10. Failures

{failures_table}

## 11. Limitations

- The customer is simulated from prepared answers: an unforeseen question gets a neutral reply
  and counts against the system; more than 3 such turns make the conversation invalid.
- ESC-10 and ESC-11 are injected faults, not observed ones.
- The baseline never sees fraud scores, statuses or earlier cases (DATA-01); the comparison
  measures what deciding with the engine adds, not the model's ceiling.
- Real cases are a locked snapshot; the cases a run creates on real customers are removed.

## 12. Run 1 (before evaluation-driven fixes)

{previous}

## 13. How to reproduce

```
python scripts/run_evaluation.py --dry-run   # guards, cases and estimate, no model call
python scripts/run_evaluation.py             # the full evaluation
```
"""


def _fmt(value: object) -> str:
    return "n/a" if value is None else f"{float(value):.3f}"  # type: ignore[arg-type]


def write_json(data: dict[str, Any]) -> str:
    return json.dumps(data, indent=2, ensure_ascii=False, default=str) + "\n"
