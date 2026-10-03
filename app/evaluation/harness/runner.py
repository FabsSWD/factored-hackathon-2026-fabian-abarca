"""Runs the evaluation (M18): guards, clean state, the system runs, the baseline, the outputs.

Each system run starts from a clean state: the seeded cases are seeded again (``SEED-`` rows
only, audit untouched) and the real customers of the evaluation have no case or card block.
The cases a run creates on real customers are recorded from the traces and removed at the end
of that run with the owner role (only those rows; ``audit_logs`` is never touched), so the next
run does not meet GATE-11.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import subprocess
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import yaml
from sqlalchemy import create_engine, delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit.cost import estimate_cost, rates_from_settings
from app.config import PolicyConfig
from app.contracts import ModelSource
from app.decision.fallback import derive_fallback
from app.decision.questions import load_kev_questions
from app.evaluation.harness import guards
from app.evaluation.harness.baseline import (
    BASELINE_PROMPT_VERSION,
    BaselinePredictor,
    context_for,
)
from app.evaluation.harness.cases import EvalCase, real_cases, seeded_cases, with_segments
from app.evaluation.harness.faults import Capture, FaultRegistry
from app.evaluation.harness.metrics import CaseTruth, Prediction, score
from app.evaluation.harness.report import (
    BASELINE,
    NO_CONNECT,
    SYSTEM,
    Arm,
    Evaluation,
    _arm_summary,
    dashboard_json,
    evaluation_markdown,
    failures,
    results_csv,
    write_json,
)
from app.evaluation.harness.server import HarnessServer, build_app
from app.evaluation.harness.signals import SignalTurn, compare
from app.evaluation.harness.simulator import (
    FINAL_KINDS,
    ConversationResult,
    CustomerSimulator,
    HttpChatClient,
)
from app.evaluation.labeler import LABEL_BUSINESS_DATE
from app.evaluation.scenarios import load_scenarios, spec_version
from app.evaluation.seed import build_rows, require_owner, require_owner_url, seed
from app.llm_adapter import prompts
from app.llm_adapter.client import LLMClientConfig, OpenAIJsonClient
from app.orchestrator.calls import collect_calls, record_call
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory
from app.storage.models import CardBlock, Case

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT = ROOT / "reports"
REASON_TURN = "clarify:reason_code"
BASELINE_ATTEMPTS = 5  # a baseline call is an error only after five attempts
# Run 1, kept as it came out: the fixes of the second run were driven by it.
PREVIOUS_RUN = "m18_evaluation_run1.json"
PREVIOUS_REPORT = "m18_run1_report.md"
ANALYSES = "m18_failure_analyses.yaml"  # hand-written analysis of each failure, by case key


@dataclass(frozen=True)
class Options:
    runs: int = 3
    connect_off_run: bool = True
    baseline: bool = True
    concurrency: int = 5
    baseline_concurrency: int = 2  # the baseline's long prompts hit OpenAI's rate limit (429)
    max_turns: int = 15
    only: frozenset[str] = frozenset()  # a subset of case keys (smoke run); empty: all
    output: Path = DEFAULT_OUTPUT
    write_docs: bool = True


@dataclass
class RunState:
    """What one system run leaves for the report."""

    label: str
    results: list[ConversationResult] = field(default_factory=list)
    capture: Capture = field(default_factory=Capture)
    cleaned: int = 0


# --- Pure conversions ----------------------------------------------------------------------------


def truth_of(case: EvalCase) -> CaseTruth:
    return CaseTruth(
        key=case.key,
        label=case.label,
        language=case.language,
        data_source=case.data_source,
        country=case.segment.country,
        age_band=case.segment.age_band,
        gender=case.segment.gender,
        fault_injected=case.faults.any,
    )


def prediction_of(result: ConversationResult) -> Prediction:
    turn_ms = tuple(t.server_ms if t.server_ms is not None else t.wall_ms for t in result.turns)
    return Prediction(
        key=result.key,
        run=result.run,
        outcome=result.outcome,
        rules=tuple(sorted(result.rules)),
        queue=result.queue,
        priority=result.priority,
        actions=tuple(result.actions),
        handed_off=result.handed_off,
        turn_ms=turn_ms,
        conversation_ms=sum(turn_ms) if turn_ms else None,
        cost_usd=result.cost_usd,
        end=result.end,
        out_of_script=len(result.out_of_script),
        error=result.error,
    )


def signal_turns(
    cases: dict[str, EvalCase], results: list[ConversationResult], capture: Capture
) -> list[SignalTurn]:
    """Kev and the extraction fallback on every captured turn, with the case's truth."""
    turns: list[SignalTurn] = []
    for result in results:
        case = cases[result.key]
        escalate = int(case.label.outcome.value == "ESCALATE")
        disputes_done = 0
        previous = ""
        for record in result.turns:
            key = (result.conversation_id, record.index)
            asked_reason = record.index == 0 or previous == REASON_TURN
            reason = None
            if asked_reason and disputes_done < len(case.reasons):
                reason = case.reasons[disputes_done]
            ambiguous = int(record.clarify_target in ("transaction_ref", "reason_code"))
            kev = capture.kev.get(key)
            if kev is not None and kev.source is ModelSource.KEV:
                turns.append(SignalTurn("kev", kev, reason, ambiguous, escalate))
            extraction = capture.extractions.get(key)
            if extraction is not None:
                fallback = derive_fallback(extraction)
                turns.append(SignalTurn("llm_fallback", fallback, reason, ambiguous, escalate))
            if record.reply_kind.startswith(FINAL_KINDS):
                disputes_done += 1
            previous = record.reply_kind
    return turns


def case_mix(cases: list[EvalCase]) -> dict[str, Any]:
    languages = Counter(c.language for c in cases)
    paths = Counter(c.path for c in cases)
    return {
        "cases": len(cases),
        "seeded": sum(c.data_source == "seeded" for c in cases),
        "real": sum(c.data_source == "real" for c in cases),
        **{f"language_{k}": v for k, v in sorted(languages.items())},
        **{f"path_{k}": v for k, v in sorted(paths.items())},
        "fault_injected": sum(c.faults.any for c in cases),
    }


# --- Database state ------------------------------------------------------------------------------


def owner_sessions(settings: Settings) -> sessionmaker[Session]:
    url = require_owner_url(settings.migration_database_url, settings.database_url)
    sessions = sessionmaker(bind=create_engine(url))
    with sessions() as session:
        require_owner(session)
    return sessions


def reseed(owner: sessionmaker[Session], settings: Settings) -> None:
    key = settings.document_hash_key.get_secret_value() if settings.document_hash_key else ""
    if not key:
        raise guards.HarnessGuardError("DOCUMENT_HASH_KEY is not set")
    rows = build_rows(load_scenarios(), settings.business_date, key, spec_version())
    with owner() as session:
        seed(session, rows)
        session.commit()


def real_state(owner: sessionmaker[Session], customer_ids: set[str]) -> tuple[int, int]:
    """Cases and card blocks the real customers of the evaluation have."""
    if not customer_ids:
        return 0, 0
    with owner() as session:
        cases = session.scalar(
            select(func.count()).select_from(Case).where(Case.customer_id.in_(customer_ids))
        )
        blocks = session.scalar(
            select(func.count())
            .select_from(CardBlock)
            .where(CardBlock.customer_id.in_(customer_ids))
        )
        session.rollback()
    return int(cases or 0), int(blocks or 0)


def remove_real_cases(
    owner: sessionmaker[Session], case_ids: set[str], customer_ids: set[str]
) -> int:
    """Delete the cases this run created on real customers, and nothing else."""
    if not case_ids or not customer_ids:
        return 0
    with owner() as session:
        removed = session.execute(
            delete(Case).where(Case.case_id.in_(case_ids), Case.customer_id.in_(customer_ids))
        ).rowcount  # type: ignore[attr-defined]
        session.commit()
    return int(removed or 0)


# --- Runs ----------------------------------------------------------------------------------------


async def simulate(
    base_url: str,
    harness_traces: Any,
    cases: list[EvalCase],
    label: str,
    faults: FaultRegistry,
    otp: str,
    options: Options,
) -> list[ConversationResult]:
    gate = asyncio.Semaphore(options.concurrency)
    async with httpx.AsyncClient(base_url=base_url, timeout=180.0) as http:
        simulator = CustomerSimulator(HttpChatClient(http, otp), harness_traces, options.max_turns)

        async def one(case: EvalCase) -> ConversationResult:
            conversation_id = f"EV-{label}-{case.key}-{uuid.uuid4().hex[:6]}"
            faults.register(conversation_id, case.faults)
            async with gate:
                return await simulator.play(case, label, conversation_id)

        return list(await asyncio.gather(*(one(case) for case in cases)))


def system_run(
    settings: Settings,
    policy: PolicyConfig,
    owner: sessionmaker[Session],
    cases: list[EvalCase],
    label: str,
    options: Options,
    progress: Callable[[str], None],
) -> RunState:
    state = RunState(label)
    reseed(owner, settings)
    real_ids = {c.customer_id for c in cases if c.data_source == "real"}
    leftover = real_state(owner, real_ids)
    if leftover != (0, 0):
        raise guards.HarnessGuardError(
            f"real customers of the evaluation have {leftover[0]} cases and {leftover[1]} card "
            "blocks: GATE-11 would change their labels"
        )
    otp = settings.test_otp.get_secret_value() if settings.test_otp else ""
    if not otp:
        raise guards.HarnessGuardError("TEST_OTP is not set")
    faults = FaultRegistry()
    harness = build_app(settings, policy, faults, state.capture)
    try:
        with HarnessServer(harness.app) as server:
            progress(f"{label}: {len(cases)} conversations")
            state.results = asyncio.run(
                simulate(server.base_url, harness, cases, label, faults, otp, options)
            )
    finally:
        harness.engine.dispose()
        created = {cid for r in state.results for cid in r.created_cases}
        state.cleaned = remove_real_cases(owner, created, real_ids)
    if real_state(owner, real_ids)[0]:
        raise guards.HarnessGuardError(f"{label}: cases remain on real customers after cleanup")
    return state


def baseline_run(
    settings: Settings,
    policy: PolicyConfig,
    cases: list[EvalCase],
    options: Options,
) -> Arm:
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    client = OpenAIJsonClient(
        LLMClientConfig(
            api_key=key,
            model=settings.llm_model or "",
            base_url=settings.openai_base_url,
            timeout_seconds=120.0,
            max_retries=BASELINE_ATTEMPTS - 1,  # waits as Retry-After says on 429
            turn_deadline_seconds=900.0,
        ),
        recorder=record_call,
    )
    predictor = BaselinePredictor(client)
    rates = rates_from_settings(settings)
    pseudonym = settings.require_pseudonym_key()
    if not settings.database_url:
        raise guards.HarnessGuardError("DATABASE_URL is not set")
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    arm = Arm(BASELINE)

    async def all_cases() -> None:
        gate = asyncio.Semaphore(options.baseline_concurrency)

        async def one(case: EvalCase) -> None:
            context = context_for(case, sessions, pseudonym, settings.as_of,
                                  policy.parameters.LATE_WINDOW_DAYS)  # fmt: skip
            async with gate:
                with collect_calls() as calls:
                    prediction, rationale = await predictor.predict(case, context)
            cost = estimate_cost(calls, rates)
            arm.predictions.append(replace(prediction, cost_usd=cost))
            arm.rationales[case.key] = rationale

        await asyncio.gather(*(one(case) for case in cases))

    try:
        asyncio.run(all_cases())
    finally:
        engine.dispose()
    return arm


def versions(
    settings: Settings, policy: PolicyConfig, states: list[RunState], options: Options
) -> dict[str, Any]:
    models = sorted({m for s in states for r in s.results for m in r.model_versions})
    used = sorted({v for s in states for r in s.results for v in r.prompt_versions})
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        ).stdout.strip()
        dirty = bool(
            subprocess.run(
                ["git", "status", "--porcelain", "--untracked-files=no"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                check=False,
            ).stdout.strip()
        )
    except OSError:
        commit, dirty = "", False
    return {
        "policy": policy.policy_version,
        "extract_prompt": prompts.EXTRACT_PROMPT_VERSION,
        "connect_prompt": prompts.CONNECT_PROMPT_VERSION,
        "baseline_prompt": BASELINE_PROMPT_VERSION,
        "kev_questions": load_kev_questions().prompt_version,
        "prompts_seen": ", ".join(used) or "n/a",
        "models_seen": ", ".join(models) or "n/a",
        "llm_model_configured": settings.llm_model or "n/a",
        "kev_configured": bool(settings.kev_base_url),
        "scenario_specs": spec_version(),
        "split_fingerprint": guards.EVALUATION_SPLIT_FINGERPRINT[:16],
        "business_date": str(settings.business_date),
        "git_commit": commit or "n/a",
        "git_dirty": dirty,  # uncommitted changes: git_commit alone does not reproduce the run
        "system_runs": options.runs,
        "concurrency": options.concurrency,
        "max_turns": options.max_turns,
    }


def load_cases(settings: Settings, evaluation: list[str], options: Options) -> list[EvalCase]:
    keys = set(evaluation)
    if options.only:
        unknown = sorted(options.only - keys)
        if unknown:
            raise guards.HarnessGuardError(f"{unknown} are not evaluation cases")
        keys &= options.only
    if not settings.database_url:
        raise guards.HarnessGuardError("DATABASE_URL is not set")
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    try:
        cases = seeded_cases(keys) + real_cases(keys, sessions)
        return with_segments(sorted(cases, key=lambda c: c.key), sessions)
    finally:
        engine.dispose()


def run_evaluation(
    settings: Settings,
    policy: PolicyConfig,
    options: Options,
    progress: Callable[[str], None] = print,
) -> Evaluation:
    if settings.business_date != LABEL_BUSINESS_DATE:
        raise guards.HarnessGuardError(f"BUSINESS_DATE must be {LABEL_BUSINESS_DATE}")
    split = guards.check_all(policy)
    started = datetime.now(UTC)
    cases = load_cases(settings, split.evaluation, options)
    owner = owner_sessions(settings)
    by_key = {c.key: c for c in cases}
    truths = {c.key: truth_of(c) for c in cases}
    states: list[RunState] = []
    arms: dict[str, Arm] = {SYSTEM: Arm(SYSTEM)}
    plan = [(SYSTEM, f"system-{n}", settings) for n in range(1, options.runs + 1)]
    if options.connect_off_run:
        arms[NO_CONNECT] = Arm(NO_CONNECT)
        quiet = settings.model_copy(update={"llm_connect_enabled": False})
        plan.append((NO_CONNECT, "no-connect-1", quiet))
    for arm_name, label, run_settings in plan:
        state = system_run(run_settings, policy, owner, cases, label, options, progress)
        states.append(state)
        arm = arms[arm_name]
        arm.cleaned_cases[label] = state.cleaned
        for result in state.results:
            prediction = prediction_of(result)
            arm.predictions.append(prediction)
            arm.scores.append(score(truths[result.key], prediction))
    if options.baseline:
        progress(f"baseline: {len(cases)} calls")
        baseline = baseline_run(settings, policy, cases, options)
        baseline.scores = [score(truths[p.key], p) for p in baseline.predictions]
        arms[BASELINE] = baseline
    turns = [t for s in states if s.label.startswith("system-") for t in
             signal_turns(by_key, s.results, s.capture)]  # fmt: skip
    signals = compare(turns)
    signals["esc11_thresholds"] = {
        "DECISION_CONFIDENCE_MIN": policy.parameters.DECISION_CONFIDENCE_MIN,
        "ESCALATION_RISK_THRESHOLD": policy.parameters.ESCALATION_RISK_THRESHOLD,
        "note": "uncalibrated: null until M7 fits them on the calibration split",
    }
    return Evaluation(
        started_at=started,
        finished_at=datetime.now(UTC),
        versions=versions(settings, policy, states, options),
        mix=case_mix(cases),
        truths=truths,
        titles={c.key: c.title for c in cases},
        paths={c.key: c.path for c in cases},
        arms=arms,
        signals=signals,
        partial=bool(options.only),
        previous=_json(options.output / PREVIOUS_RUN),
        previous_report=_text(options.output / PREVIOUS_REPORT),
        review=_json(options.output / "m17_review_summary.json"),
        analyses=_analyses(options.output / ANALYSES),
    )


def _json(path: Path) -> dict[str, Any] | None:
    return dict(json.loads(path.read_text(encoding="utf-8"))) if path.exists() else None


def _analyses(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(key): str(value) for key, value in (raw.get("analyses") or {}).items()}


def _text(path: Path) -> str | None:
    return path.read_text(encoding="utf-8") if path.exists() else None


def write_outputs(evaluation: Evaluation, options: Options) -> list[Path]:
    folder = options.output / "m18"
    folder.mkdir(parents=True, exist_ok=True)
    written = []
    csv_path = folder / "results.csv"  # git-ignored: per-case segments
    csv_path.write_text(results_csv(evaluation), encoding="utf-8", newline="\n")
    written.append(csv_path)
    name = "m18_evaluation_partial.json" if evaluation.partial else "m18_evaluation.json"
    json_path = options.output / name
    json_path.write_text(write_json(dashboard_json(evaluation)), encoding="utf-8", newline="\n")
    written.append(json_path)
    if options.write_docs and not evaluation.partial:
        doc = ROOT / "docs" / "evaluation.md"
        doc.write_text(evaluation_markdown(evaluation), encoding="utf-8", newline="\n")
        written.append(doc)
    return written


def _prediction_from_row(row: dict[str, str]) -> Prediction:
    """A baseline prediction as results.csv keeps it (one call: its time is its only turn)."""
    elapsed = float(row["conversation_ms"]) if row["conversation_ms"] else None
    return Prediction(
        key=row["case_key"],
        run=row["run"],
        outcome=row["outcome"] or None,
        rules=tuple(row["rules"].split()),
        queue=row["queue"] or None,
        priority=row["priority"] or None,
        conversation_ms=elapsed,
        turn_ms=(elapsed,) if elapsed is not None and not row["error"] else (),  # as predicted
        cost_usd=Decimal(row["cost_usd"]) if row["cost_usd"] else None,
        end=row["end"],
        out_of_script=int(row["out_of_script"] or 0),
        error=row["error"] or None,
    )


def retry_baseline_failures(
    settings: Settings,
    policy: PolicyConfig,
    options: Options,
    progress: Callable[[str], None] = print,
) -> list[Path]:
    """Call the baseline again for the cases whose call failed, and merge the answers into the
    outputs of the last run: the baseline's section of the JSON, its rows of results.csv and
    docs/evaluation.md. The system's arms are left exactly as they were."""
    json_path = options.output / "m18_evaluation.json"
    csv_path = options.output / "m18" / "results.csv"
    if not json_path.exists() or not csv_path.exists():
        raise guards.HarnessGuardError("no evaluation to merge into: run the evaluation first")
    split = guards.check_all(policy)
    data = json.loads(json_path.read_text(encoding="utf-8"))
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        header = list(reader.fieldnames or [])
        rows = list(reader)
    kept = [row for row in rows if row["arm"] != BASELINE]
    predictions = {
        row["case_key"]: _prediction_from_row(row) for row in rows if row["arm"] == BASELINE
    }
    rationales = {row["case_key"]: row["rationale"] for row in rows if row["arm"] == BASELINE}
    cases = load_cases(settings, split.evaluation, options)
    by_key = {c.key: c for c in cases}
    failed = sorted(key for key, p in predictions.items() if p.error is not None)
    if failed:
        progress(f"baseline: calling again {', '.join(failed)}")
        again = baseline_run(settings, policy, [by_key[key] for key in failed], options)
        for prediction in again.predictions:
            predictions[prediction.key] = prediction
            rationales[prediction.key] = again.rationales.get(prediction.key, "")
    truths = {c.key: truth_of(c) for c in cases}
    arm = Arm(BASELINE, sorted(predictions.values(), key=lambda p: p.key), rationales=rationales)
    arm.scores = [score(truths[p.key], p) for p in arm.predictions]
    merged = Evaluation(
        started_at=datetime.fromisoformat(data["started_at"]),
        finished_at=datetime.fromisoformat(data["generated_at"]),
        versions=data["versions"],
        mix=data["mix"],
        truths=truths,
        titles={c.key: c.title for c in cases},
        paths={c.key: c.path for c in cases},
        arms={BASELINE: arm},
        signals=data.get("learned_component"),
        partial=bool(data.get("partial")),
        previous=_json(options.output / PREVIOUS_RUN),
        previous_report=_text(options.output / PREVIOUS_REPORT),
        review=_json(options.output / "m17_review_summary.json"),
        analyses=_analyses(options.output / ANALYSES),
    )
    data["arms"][BASELINE] = _arm_summary(merged, arm)
    data["failures"] = [f for f in data["failures"] if f["arm"] != BASELINE] + failures(merged)
    if failed:  # nothing failed: the outputs are only rendered again (the analyses may be new)
        data["baseline_retry"] = {"retried": failed, "at": datetime.now(UTC).isoformat()}
    json_path.write_text(write_json(data), encoding="utf-8", newline="\n")
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=header, lineterminator="\n")
    writer.writeheader()
    writer.writerows(kept)
    baseline_rows = results_csv(merged).splitlines()[1:]  # same columns, baseline only
    csv_path.write_text(out.getvalue() + "\n".join(baseline_rows) + "\n", encoding="utf-8",
                        newline="\n")  # fmt: skip
    written = [json_path, csv_path]
    if options.write_docs and not merged.partial:
        doc = ROOT / "docs" / "evaluation.md"
        doc.write_text(evaluation_markdown(merged, data), encoding="utf-8", newline="\n")
        written.append(doc)
    return written


__all__ = [
    "Options",
    "case_mix",
    "prediction_of",
    "run_evaluation",
    "signal_turns",
    "truth_of",
    "write_outputs",
]
