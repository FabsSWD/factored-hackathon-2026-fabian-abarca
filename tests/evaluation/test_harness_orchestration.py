"""How the harness orchestrates runs, with every external piece replaced by a double: the
conversations over real HTTP against a fake API, the system runs (seeding, the server, the
cleanup of real customers), the baseline calls and the assembled evaluation."""

from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import load_policy_config
from app.contracts import Outcome, TraceRecord
from app.evaluation.harness import guards, runner
from app.evaluation.harness.cases import seeded_cases
from app.evaluation.harness.faults import Capture, FaultRegistry
from app.evaluation.harness.guards import HarnessGuardError
from app.evaluation.harness.metrics import Prediction, score
from app.evaluation.harness.report import BASELINE, NO_CONNECT, SYSTEM, Arm
from app.evaluation.harness.server import HarnessServer
from app.evaluation.harness.simulator import ConversationResult, TurnRecord
from app.llm_adapter.client import JsonCompletion
from app.settings import Settings

POLICY = load_policy_config()
NO_OWNER: Any = None  # the owner role is never reached: its users are doubles
NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
SETTINGS = Settings(
    _env_file=None,
    business_date=date(2026, 6, 17),
    database_url="postgresql+psycopg://u:p@localhost:1/none",
    test_otp=SecretStr("123456"),
    openai_api_key=SecretStr("sk-test"),
    llm_model="gpt-6-luna",
    pseudonym_key=SecretStr("k"),
)


def fake_api() -> tuple[FastAPI, dict[str, TraceRecord], list[dict[str, Any]]]:
    app = FastAPI()
    traces: dict[str, TraceRecord] = {}
    seen: list[dict[str, Any]] = []

    @app.post("/auth/login", status_code=202)
    def login(body: dict[str, Any]) -> dict[str, Any]:
        return {"status": "otp_sent", "expires_in_seconds": 300}

    @app.post("/auth/verify")
    def verify(body: dict[str, Any]) -> dict[str, Any]:
        return {"access_token": f"tok-{body['document_number']}"}

    @app.post("/api/turn")
    def turn(body: dict[str, Any]) -> dict[str, Any]:
        seen.append(body)
        trace_id = f"TRC-{len(seen)}"
        traces[trace_id] = TraceRecord(
            trace_id=trace_id,
            conversation_id=body["conversation_id"],
            turn_index=0,
            created_at=NOW,
            outcome=Outcome.INFORM,
            reply_kind="inform:transaction_pending",
            policy_version="0.4.11",
        )
        return {
            "conversation_id": body["conversation_id"],
            "turn_index": 0,
            "reply": "ok",
            "handed_off": False,
            "trace_id": trace_id,
        }

    return app, traces, seen


class Traces:
    def __init__(self, traces: dict[str, TraceRecord]) -> None:
        self._traces = traces

    def trace(self, trace_id: str) -> TraceRecord | None:
        return self._traces.get(trace_id)

    def handoff(self, handoff_id: str) -> None:
        return None


def test_conversations_run_over_http_with_their_faults_registered() -> None:
    app, traces, seen = fake_api()
    cases = seeded_cases({"S001", "S090", "S091"})
    faults = FaultRegistry()
    with HarnessServer(app) as server:
        results = asyncio.run(
            runner.simulate(
                server.base_url,
                Traces(traces),
                cases,
                "system-1",
                faults,
                "123456",
                runner.Options(concurrency=2),
            )
        )
    assert sorted(r.key for r in results) == ["S001", "S090", "S091"]
    assert all(r.end == "final" and r.outcome == "INFORM" for r in results)
    assert all(b["conversation_id"].startswith("EV-system-1-S0") for b in seen)
    planned = {cid.split("-")[3]: plan for cid, plan in faults.plans.items()}
    assert planned["S090"].tool_write_failure and planned["S091"].models_down
    assert not planned["S001"].any


def finished(key: str, run: str, case_id: str | None = None) -> ConversationResult:
    r = ConversationResult(
        key, run, f"EV-{run}-{key}", end="final", outcome="RESOLVE", actions=["ACT-02", "ACT-04"]
    )
    r.turns = [TurnRecord(0, "case_created", "RESOLVE", 100.0, 120.0, Decimal("0.01"), False)]
    if case_id:
        r.created_cases.append(case_id)
    return r


def test_a_system_run_seeds_serves_and_cleans(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    real = seeded_cases({"S001"})[0]
    real = real.__class__(**{**real.__dict__, "data_source": "real", "customer_id": "CLI-REAL"})
    states = iter([(0, 0), (0, 0)])
    monkeypatch.setattr(runner, "reseed", lambda owner, settings: calls.append("reseed"))
    monkeypatch.setattr(runner, "real_state", lambda owner, ids: next(states))

    def remove(owner: Any, created: set[str], ids: set[str]) -> int:
        calls.append(f"remove {sorted(created)} {sorted(ids)}")
        return len(created)

    class FakeHarness:
        app = FastAPI()

        class engine:  # noqa: N801
            @staticmethod
            def dispose() -> None:
                calls.append("dispose")

    async def simulate(*args: Any) -> list[ConversationResult]:
        return [finished("S001", "system-1", "DSP-1")]

    monkeypatch.setattr(runner, "remove_real_cases", remove)
    monkeypatch.setattr(runner, "build_app", lambda *args: FakeHarness())
    monkeypatch.setattr(runner, "simulate", simulate)
    state = runner.system_run(
        SETTINGS, POLICY, NO_OWNER, [real], "system-1", runner.Options(), calls.append
    )
    assert state.cleaned == 1 and state.results[0].key == "S001"
    assert calls[0] == "reseed" and "remove ['DSP-1'] ['CLI-REAL']" in calls and "dispose" in calls

    monkeypatch.setattr(runner, "real_state", lambda owner, ids: (2, 0))
    with pytest.raises(HarnessGuardError, match="GATE-11"):
        runner.system_run(SETTINGS, POLICY, NO_OWNER, [real], "system-1", runner.Options(), print)
    monkeypatch.setattr(runner, "real_state", lambda owner, ids: (0, 0))
    no_otp = SETTINGS.model_copy(update={"test_otp": None})
    with pytest.raises(HarnessGuardError, match="TEST_OTP"):
        runner.system_run(no_otp, POLICY, NO_OWNER, [real], "system-1", runner.Options(), print)
    after = iter([(0, 0), (1, 0)])
    monkeypatch.setattr(runner, "real_state", lambda owner, ids: next(after))
    with pytest.raises(HarnessGuardError, match="remain"):
        runner.system_run(SETTINGS, POLICY, NO_OWNER, [real], "system-1", runner.Options(), print)


def test_the_baseline_run_prices_each_call(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.contracts import LLMContext
    from app.evaluation.harness.baseline import BaselineRecords
    from app.orchestrator.calls import record_call

    class FakeClient:
        def __init__(self, config: Any, recorder: Any) -> None:
            self.recorder = recorder
            configs.append(config)

        async def complete_json(self, **kwargs: Any) -> JsonCompletion:
            from app.contracts import ModelCall

            call = ModelCall(
                provider="openai",
                model="gpt-6-luna",
                purpose="baseline_decision",
                input_tokens=1_000_000,
                output_tokens=0,
                latency_ms=1.0,
                success=True,
            )
            record_call(call)
            answer: dict[str, Any] = {
                "outcome": "RESOLVE",
                "triggered_rules": [],
                "queue": None,
                "priority": None,
                "rationale": "ok",
            }
            return JsonCompletion(answer, answer, 1_000_000, 0)

    configs: list[Any] = []
    priced = SETTINGS.model_copy(
        update={"llm_input_usd_per_mtok": Decimal("2"), "llm_output_usd_per_mtok": Decimal("8")}
    )
    monkeypatch.setattr(runner, "OpenAIJsonClient", FakeClient)
    monkeypatch.setattr(
        runner, "context_for", lambda *args: BaselineRecords(LLMContext(customer_ref="CUS-x"))
    )
    arm = runner.baseline_run(priced, POLICY, seeded_cases({"S001", "S002"}), runner.Options())
    assert sorted(p.key for p in arm.predictions) == ["S001", "S002"]
    assert all(p.cost_usd == Decimal("2") for p in arm.predictions)
    assert arm.rationales == {"S001": "ok", "S002": "ok"}
    assert configs[0].max_retries == 4  # five attempts before a 429 is an error
    with pytest.raises(HarnessGuardError, match="DATABASE_URL"):
        runner.baseline_run(
            SETTINGS.model_copy(update={"database_url": None}), POLICY, [], runner.Options()
        )


def test_the_evaluation_is_assembled(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    cases = seeded_cases({"S001", "S002"})
    monkeypatch.setattr(guards, "check_all", lambda policy: guards.stored_split())
    monkeypatch.setattr(runner, "load_cases", lambda settings, evaluation, options: cases)
    monkeypatch.setattr(runner, "owner_sessions", lambda settings: None)
    labels: list[str] = []

    def system_run(
        settings: Settings,
        policy: Any,
        owner: Any,
        cs: Any,
        label: str,
        options: Any,
        progress: Any,
    ) -> runner.RunState:
        labels.append(f"{label}:{settings.llm_connect_enabled}")
        state = runner.RunState(label, [finished(c.key, label) for c in cs], Capture())
        state.cleaned = 0
        return state

    def baseline_run(settings: Settings, policy: Any, cs: Any, options: Any) -> Arm:
        arm = Arm(BASELINE)
        arm.predictions = [Prediction(c.key, "baseline", "INFORM") for c in cs]
        return arm

    monkeypatch.setattr(runner, "system_run", system_run)
    monkeypatch.setattr(runner, "baseline_run", baseline_run)
    e = runner.run_evaluation(SETTINGS, POLICY, runner.Options(runs=2, output=tmp_path), print)
    assert labels == ["system-1:True", "system-2:True", "no-connect-1:False"]
    assert set(e.arms) == {SYSTEM, NO_CONNECT, BASELINE}
    assert len(e.arms[SYSTEM].scores) == 4 and all(s.full_ok for s in e.arms[SYSTEM].scores)
    assert not any(s.outcome_ok for s in e.arms[BASELINE].scores)
    assert (
        e.signals is not None and e.signals["esc11_thresholds"]["ESCALATION_RISK_THRESHOLD"] is None
    )
    assert e.mix["cases"] == 2 and not e.partial
    written = runner.write_outputs(e, runner.Options(output=tmp_path, write_docs=False))
    assert written[1].read_text(encoding="utf-8").startswith("{")


def test_the_owner_role_is_required() -> None:
    with pytest.raises(Exception, match=r"MIGRATION_DATABASE_URL|not set|owner"):
        runner.owner_sessions(SETTINGS)


def test_failed_baseline_calls_are_retried_and_merged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.evaluation.test_harness_metrics import RESOLVE, evaluation

    e = evaluation()
    e.arms[BASELINE].predictions[0] = Prediction("S001", "baseline", None, end="error", error="429")
    e.arms[BASELINE].scores[0] = score(RESOLVE, e.arms[BASELINE].predictions[0])
    runner.write_outputs(e, runner.Options(output=tmp_path, write_docs=False))
    before = (tmp_path / "m18_evaluation.json").read_text(encoding="utf-8")
    cases = [
        c.__class__(**{**c.__dict__, "key": key})
        for c, key in zip(seeded_cases({"S001", "S071"}), ("S001", "S071"), strict=True)
    ]
    monkeypatch.setattr(guards, "check_all", lambda policy: guards.stored_split())
    monkeypatch.setattr(runner, "load_cases", lambda settings, evaluation, options: cases)
    monkeypatch.setattr(runner, "truth_of", lambda case: e.truths[case.key])
    called: list[str] = []

    def baseline_run(settings: Settings, policy: Any, cs: Any, options: Any) -> Arm:
        called.extend(c.key for c in cs)
        arm = Arm(BASELINE)
        arm.predictions = [Prediction(c.key, "baseline", "RESOLVE", turn_ms=(10.0,)) for c in cs]
        arm.rationales = {c.key: "retried" for c in cs}
        return arm

    monkeypatch.setattr(runner, "baseline_run", baseline_run)
    options = runner.Options(output=tmp_path, write_docs=False)
    written = runner.retry_baseline_failures(SETTINGS, POLICY, options, print)
    assert called == ["S001"]  # only the failed call
    import json as _json

    old, new = _json.loads(before), _json.loads(written[0].read_text(encoding="utf-8"))
    assert new["arms"][SYSTEM] == old["arms"][SYSTEM]  # the system's arm untouched
    assert new["arms"][BASELINE]["pooled"]["errors"] == 0
    assert new["baseline_retry"]["retried"] == ["S001"]
    rows = written[1].read_text(encoding="utf-8").splitlines()
    assert (
        sum(r.startswith("system,") for r in rows) == 4
        and sum(r.startswith("baseline,") for r in rows) == 2
    )
    again = runner.retry_baseline_failures(SETTINGS, POLICY, options, print)  # nothing failed
    assert (
        called == ["S001"] and _json.loads(again[0].read_text(encoding="utf-8"))["baseline_retry"]
    )
    with pytest.raises(HarnessGuardError, match="run the evaluation first"):
        runner.retry_baseline_failures(
            SETTINGS, POLICY, runner.Options(output=tmp_path / "x"), print
        )
