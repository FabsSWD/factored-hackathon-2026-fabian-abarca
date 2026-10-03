"""The harness refuses to run on a changed split, on calibration with evaluation cases, or on a
real selection other than the locked one; the fault wrappers touch only their conversation; the
in-process server serves the real routes."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr

from app.config import load_policy_config
from app.contracts import (
    ActionId,
    ExtractionResult,
    LLMContext,
    ModelSignals,
    ModelSource,
    ReasonCode,
    Tier,
    ToolStatus,
)
from app.evaluation.harness import guards
from app.evaluation.harness.faults import (
    CURRENT_TURN,
    Capture,
    FailingWriteTools,
    FaultPlan,
    FaultRegistry,
    HarnessDecision,
    HarnessLLM,
    harness_tools,
)
from app.evaluation.harness.server import HarnessServer, build_app
from app.evaluation.real import load_lock
from app.evaluation.split import Split
from app.llm_adapter.adapter import ExtractionUnavailableError
from app.llm_adapter.client import LLMError
from app.settings import Settings

POLICY = load_policy_config()
CONTEXT = LLMContext(customer_ref="CUS-x")


# --- Guards -----------------------------------------------------------------------------------


def test_the_repository_split_is_the_pinned_one() -> None:
    stored = guards.stored_split()
    guards.check_split(stored, guards.fresh_split(load_lock()))
    assert stored.fingerprint == guards.EVALUATION_SPLIT_FINGERPRINT


def test_a_changed_split_is_refused() -> None:
    stored = guards.stored_split()
    moved = Split(
        seed=stored.seed,
        calibration=stored.calibration[1:],
        evaluation=sorted([*stored.evaluation, stored.calibration[0]]),
    )
    with pytest.raises(guards.HarnessGuardError, match="differs"):
        guards.check_split(stored, moved)
    with pytest.raises(guards.HarnessGuardError, match="pinned"):
        guards.check_split(moved, moved)
    with pytest.raises(guards.HarnessGuardError, match="lock"):
        guards.fresh_split(None)


def test_calibration_on_an_evaluation_case_is_refused(tmp_path: Path) -> None:
    evaluation = guards.stored_split().evaluation
    log = tmp_path / "calibration_log.yaml"
    log.write_text(
        f"entries:\n- parameter: ESCALATION_RISK_THRESHOLD\n  value: 0.7\n"
        f"  cases: [{evaluation[0]}]\n",
        encoding="utf-8",
    )
    with pytest.raises(guards.HarnessGuardError, match="held out"):
        guards.check_calibration(guards.load_calibration_log(log), evaluation, POLICY)
    assert guards.load_calibration_log() == []  # the versioned log: nothing calibrated yet
    guards.check_calibration([], evaluation, POLICY)
    with pytest.raises(guards.HarnessGuardError, match="missing"):
        guards.load_calibration_log(tmp_path / "none.yaml")


def test_a_calibrated_parameter_needs_its_record() -> None:
    parameters = POLICY.parameters.model_copy(update={"ESCALATION_RISK_THRESHOLD": 0.7})
    policy = POLICY.model_copy(update={"parameters": parameters})
    with pytest.raises(guards.HarnessGuardError, match="does not say"):
        guards.check_calibration([], [], policy)
    entry = guards.CalibrationEntry(parameter="ESCALATION_RISK_THRESHOLD", cases=["S001"])
    guards.check_calibration([entry], ["S002"], policy)


def test_the_real_selection_must_be_the_locked_one(monkeypatch: pytest.MonkeyPatch) -> None:
    lock = load_lock()
    guards.check_real_selection(lock, needs_real=False)
    with pytest.raises(guards.HarnessGuardError, match="lock"):
        guards.check_real_selection(None, needs_real=True)
    monkeypatch.setattr(guards, "load_real_scenarios", lambda: [])
    with pytest.raises(guards.HarnessGuardError, match="no real cases"):
        guards.check_real_selection(lock, needs_real=True)
    monkeypatch.setattr(guards, "load_real_scenarios", lambda: ["local"])
    monkeypatch.setattr(guards, "selection_digest", lambda local: "0" * 64)
    with pytest.raises(guards.HarnessGuardError, match="ids_sha256"):
        guards.check_real_selection(lock, needs_real=True)
    assert lock is not None
    monkeypatch.setattr(guards, "selection_digest", lambda local: lock.ids_sha256)
    assert guards.check_all(POLICY).fingerprint == guards.EVALUATION_SPLIT_FINGERPRINT


# --- Fault wrappers ---------------------------------------------------------------------------


class Model:
    def __init__(self) -> None:
        self.calls = 0

    async def extract(
        self, message: str, context: LLMContext, deadline: Any = None
    ) -> ExtractionResult:
        self.calls += 1
        if message == "fails":
            raise ExtractionUnavailableError("down", ExtractionResult())
        return ExtractionResult(detected_language="es")

    async def connect(
        self,
        text: str,
        message: str,
        context: LLMContext,
        deadline: Any = None,
        *,
        brief: bool = False,
        previous: Any = (),
    ) -> str:
        return f"bien. {text}"

    async def signals(
        self, message: str, context: LLMContext, deadline: Any = None
    ) -> ModelSignals:
        return ModelSignals(source=ModelSource.KEV, ambiguity=0.5)


def test_models_down_only_for_the_planned_conversation() -> None:
    faults, capture, model = FaultRegistry(), Capture(), Model()
    faults.register("EV-down", FaultPlan(models_down=True))
    llm, kev = HarnessLLM(model, faults, capture), HarnessDecision(model, faults, capture)

    async def scenario() -> None:
        mark = CURRENT_TURN.set(("EV-down", 0))
        with pytest.raises(ExtractionUnavailableError):
            await llm.extract("quiero hablar con una persona", CONTEXT)
        with pytest.raises(LLMError):
            await llm.connect("texto", "m", CONTEXT)
        assert (await kev.signals("m", CONTEXT)).source is ModelSource.UNAVAILABLE
        CURRENT_TURN.reset(mark)
        mark = CURRENT_TURN.set(("EV-fine", 3))
        assert (await llm.extract("hola", CONTEXT)).detected_language == "es"
        assert await llm.connect("texto", "m", CONTEXT) == "bien. texto"
        assert (await kev.signals("m", CONTEXT)).source is ModelSource.KEV
        with pytest.raises(ExtractionUnavailableError):
            await llm.extract("fails", CONTEXT)  # a real failure is kept too
        CURRENT_TURN.reset(mark)
        await llm.extract("hola", CONTEXT)  # outside a turn: nothing captured

    asyncio.run(scenario())
    assert model.calls == 3  # the planned conversation never reached the model
    assert capture.extractions[("EV-down", 0)].flags.human_requested  # the rule fallback
    assert set(capture.kev) == {("EV-down", 0), ("EV-fine", 3)}
    assert ("EV-fine", 3) in capture.extractions


class Layer:
    def create_case(self, transaction_id: str, reason_code: ReasonCode, tier: Tier) -> str:
        return "real"

    def list_products(self) -> list[str]:
        return ["card"]


def test_the_write_fault_fails_act02_and_keeps_the_rest() -> None:
    failing = FailingWriteTools(Layer(), attempts=3)  # type: ignore[arg-type]
    result = failing.create_case("SEED-T001-t1", ReasonCode.FEE, Tier.T1)
    assert (result.action, result.status, result.attempts) == (
        ActionId.CREATE_CASE,
        ToolStatus.FAILED,
        3,
    )
    assert failing.list_products() == ["card"]
    faults = FaultRegistry()
    faults.register("EV-fail", FaultPlan(tool_write_failure=True))
    build = harness_tools(lambda session, cid: Layer(), faults, 3)  # type: ignore[arg-type,return-value]
    assert isinstance(build(None, "EV-fail"), FailingWriteTools)
    assert isinstance(build(None, "EV-other"), Layer)
    assert FaultPlan(models_down=True).any and not FaultPlan().any


# --- The in-process server --------------------------------------------------------------------


def test_the_server_serves_over_http() -> None:
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"ok": "yes"}

    with HarnessServer(app) as server:
        assert server.base_url.startswith("http://127.0.0.1:")
        assert httpx.get(f"{server.base_url}/ping").json() == {"ok": "yes"}


def test_the_harness_app_is_the_real_one_with_the_wrappers() -> None:
    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        database_url="postgresql+psycopg://user:pass@localhost:1/none",
        pseudonym_key=SecretStr("k"),
        jwt_secret=SecretStr("j" * 32),
        test_otp=SecretStr("123456"),
        document_hash_key=SecretStr("h"),
        openai_api_key=SecretStr("sk-test"),
        llm_model="gpt-6-luna",
    )
    harness = build_app(settings, POLICY, FaultRegistry(), Capture())
    try:
        assert set(harness.app.openapi()["paths"]) >= {"/api/turn", "/auth/login"}
    finally:
        harness.engine.dispose()
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        build_app(
            Settings(_env_file=None, business_date=date(2026, 6, 17)),
            POLICY,
            FaultRegistry(),
            Capture(),
        )
