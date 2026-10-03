"""Cases, baseline and run state of the M18 harness, on the test database with doubles."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr
from sqlalchemy import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.config import load_policy_config
from app.contracts import LLMContext
from app.evaluation.harness import runner
from app.evaluation.harness.baseline import (
    BASELINE_PROMPT_VERSION,
    BaselinePredictor,
    BaselineRecords,
    context_for,
    system_prompt,
    user_message,
    validate,
)
from app.evaluation.harness.cases import (
    CaseLoadError,
    Segment,
    real_cases,
    seeded_cases,
    with_segments,
)
from app.evaluation.harness.guards import HarnessGuardError
from app.evaluation.harness.simulator import ConversationResult, TurnRecord
from app.evaluation.real import RealScenario
from app.evaluation.seed import count_seeded
from app.llm_adapter.client import JsonCompletion
from app.settings import Settings
from app.storage.models import Case
from tests.conftest import TEST_HASH_KEY, Pipeline
from tests.fixtures.core_banking import CUSTOMER

POLICY = load_policy_config()
AS_OF = datetime(2026, 6, 18, 6, 0)


@pytest.fixture
def sessions(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


def real(transaction_id: str = "TRX-T1-PURCHASE", reference: str = "description") -> RealScenario:
    text = "{reference}" if reference == "id" else "{transaction}"
    return RealScenario.model_validate(
        {
            "id": "R001",
            "title": "t",
            "category": "unrecognized_t1",
            "language": "es",
            "path": "resolution",
            "data_source": "real",
            "customer_id": CUSTOMER,
            "transaction_id": transaction_id,
            "reference": reference,
            "dispute": {
                "reason_code": "RC_UNRECOGNIZED",
                "card_in_possession": True,
                "shared_credentials": False,
                "block": "declined",
            },
            "script": {"first": f"Hola, es {text}", "answers": {"transaction_ref": f"Fue {text}"}},
        }
    )


# --- Cases ------------------------------------------------------------------------------------


def test_seeded_cases_and_their_transcript() -> None:
    cases = {c.key: c for c in seeded_cases({"S001", "S044", "S076"})}
    assert cases["S044"].references == ("SEED-T044-t1",)  # named by its reference
    assert cases["S001"].references == () and cases["S001"].document == "SEED-0001"
    transcript = cases["S076"].transcript()
    assert transcript[0] == cases["S076"].script.first
    assert transcript[-2:] == [
        cases["S076"].script.answers["transaction_ref_2"],
        cases["S076"].script.answers["transaction_ref_3"],
    ]
    assert "SEED-C001" not in repr(cases["S001"]) and "SEED-0001" not in repr(cases["S001"])


def test_real_cases_resolve_at_run_time(sessions: sessionmaker[Session]) -> None:
    loaded = real_cases({"R001"}, sessions, documents=lambda cid: "X1234567", local=[real()])
    (case,) = loaded
    assert case.data_source == "real" and case.document == "X1234567"
    assert "Cafe Sintetico" in case.script.first and "{transaction}" not in case.script.first
    assert case.label.case_id == "R001"  # the label comes from the lock
    by_id = real_cases({"R001"}, sessions, documents=lambda cid: "X", local=[real(reference="id")])
    assert by_id[0].references == ("TRX-T1-PURCHASE",)
    with pytest.raises(CaseLoadError, match=r"customers\.csv"):
        real_cases({"R001"}, sessions, documents=lambda cid: None, local=[real()])
    with pytest.raises(CaseLoadError, match="local"):
        real_cases({"R001"}, sessions, documents=lambda cid: "X", local=[])
    assert real_cases({"S001"}, sessions, local=[]) == []


def test_segments_come_from_the_database(sessions: sessionmaker[Session]) -> None:
    cases = real_cases({"R001"}, sessions, documents=lambda cid: "X", local=[real()])
    (with_segment,) = with_segments(cases, sessions)
    assert with_segment.segment != Segment() and with_segment.segment.country != "unknown"
    nobody = seeded_cases({"S001"})  # not seeded in this database
    assert with_segments(nobody, sessions)[0].segment == Segment()


# --- Baseline ---------------------------------------------------------------------------------


def test_the_baseline_sees_what_the_model_may_see(sessions: sessionmaker[Session]) -> None:
    (case,) = real_cases({"R001"}, sessions, documents=lambda cid: "X1234567", local=[real()])
    records = context_for(
        case, sessions, "pseudonym-key", AS_OF, POLICY.parameters.LATE_WINDOW_DAYS
    )
    context = records.context
    assert context.customer_ref.startswith("CUS-") and CUSTOMER not in context.customer_ref
    assert any(t.transaction_ref == "TRX-T1-PURCHASE" for t in context.transactions)
    message = json.loads(user_message(case, records))
    assert message["customer_messages"][0] == case.script.first
    assert "X1234567" not in user_message(case, records)  # never the document
    assert set(message["records"]) == {
        "customer_ref",
        "language",
        "masked_products",
        "transactions",
        "business_date",
    }
    t1 = next(
        t for t in message["records"]["transactions"] if t["transaction_ref"] == "TRX-T1-PURCHASE"
    )
    assert t1["amount_usd"] == "50.00"  # baseline@1.1.0: the tier is decided in USD
    anonymous = seeded_cases({"S058"})[0]  # authentication declined: nothing is read
    empty = context_for(anonymous, sessions, "k", AS_OF, 120)
    assert empty.context.transactions == [] and empty.context.customer_ref == "CUS-unauthenticated"
    assert empty.amount_usd == {}


def test_the_baseline_answer_is_validated() -> None:
    assert validate({"outcome": "ESCALATE", "triggered_rules": ["ESC-03"]})
    with pytest.raises(ValueError, match="outcome"):
        validate({"outcome": "MAYBE", "triggered_rules": []})
    with pytest.raises(ValueError, match="rule"):
        validate({"outcome": "ESCALATE", "triggered_rules": ["ESC-99"]})
    assert system_prompt("POLICY TEXT").endswith("POLICY TEXT")
    assert "# Dispute policy" in system_prompt()


class Completer:
    def __init__(self, answer: dict[str, Any] | None) -> None:
        self.answer, self.requests = answer, []  # type: ignore[var-annotated]

    async def complete_json(self, **kwargs: Any) -> JsonCompletion:
        self.requests.append(kwargs)
        if self.answer is None:
            raise RuntimeError("no answer")
        return JsonCompletion(self.answer, validate(self.answer), 100, 20)


def test_the_baseline_prediction() -> None:
    case = seeded_cases({"S071"})[0]
    context = LLMContext(customer_ref="CUS-x")
    good = Completer(
        {
            "outcome": "ESCALATE",
            "triggered_rules": ["ESC-03", "ESC-03"],
            "queue": "fraud",
            "priority": "high",
            "rationale": "x" * 500,
        }
    )
    prediction, rationale = asyncio.run(
        BaselinePredictor(good, "P").predict(case, BaselineRecords(context))
    )
    assert (prediction.outcome, prediction.rules, prediction.queue) == (
        "ESCALATE",
        ("ESC-03",),
        "fraud",
    )
    assert len(rationale) == 300 and prediction.actions is None
    request = good.requests[0]
    assert request["prompt_version"] == BASELINE_PROMPT_VERSION and request["system"].endswith("P")
    failed, _ = asyncio.run(
        BaselinePredictor(Completer(None), "P").predict(case, BaselineRecords(context))
    )
    assert failed.outcome is None and failed.end == "error" and "no answer" in (failed.error or "")


# --- Runner pieces ----------------------------------------------------------------------------


def result(key: str = "S001") -> ConversationResult:
    r = ConversationResult(
        key, "system-1", f"EV-{key}", end="final", outcome="RESOLVE", actions=["ACT-02", "ACT-04"]
    )
    r.turns = [
        TurnRecord(0, "clarify:reason_code", "CLARIFY", 1000.0, 1100.0, Decimal("0.01"), False),
        TurnRecord(1, "case_created", "RESOLVE", None, 900.0, Decimal("0.01"), False),
    ]
    return r


def test_conversions_and_mix() -> None:
    cases = seeded_cases({"S001", "S090", "S091"})
    truth = runner.truth_of(cases[1])
    assert truth.fault_injected and truth.key == "S090"
    prediction = runner.prediction_of(result())
    assert prediction.turn_ms == (1000.0, 900.0) and prediction.conversation_ms == 1900.0
    assert prediction.cost_usd == Decimal("0.02") and prediction.handed_off is False
    mix = runner.case_mix(cases)
    assert mix["cases"] == 3 and mix["fault_injected"] == 2 and mix["seeded"] == 3


def test_the_versions_report(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(_env_file=None, business_date=date(2026, 6, 17), llm_model="gpt-6-luna")
    state = runner.RunState("system-1", [result()])
    state.results[0].model_versions = ["openai:gpt-6-luna-2026"]
    out = runner.versions(settings, POLICY, [state], runner.Options())
    assert out["policy"] == POLICY.policy_version and out["models_seen"] == "openai:gpt-6-luna-2026"
    assert out["split_fingerprint"] and out["kev_configured"] is False
    assert isinstance(out["git_dirty"], bool)  # git_commit alone may not reproduce the run


def test_clean_state_between_runs(sessions: sessionmaker[Session], db_session: Session) -> None:
    settings = Settings(
        _env_file=None, business_date=date(2026, 6, 17), document_hash_key=SecretStr(TEST_HASH_KEY)
    )
    runner.reseed(sessions, settings)
    assert count_seeded(db_session)["customers"] > 0
    with pytest.raises(HarnessGuardError, match="DOCUMENT_HASH_KEY"):
        runner.reseed(sessions, Settings(_env_file=None, business_date=date(2026, 6, 17)))
    assert runner.real_state(sessions, {CUSTOMER}) == (0, 0)
    assert runner.real_state(sessions, set()) == (0, 0)
    db_session.add(
        Case(
            case_id="DSP-20261003-000001",
            customer_id=CUSTOMER,
            transaction_id="TRX-T1-PURCHASE",
            reason_code="RC_UNRECOGNIZED",
            status="Open",
            tier="T1",
            amount=Decimal("50"),
            currency="USD",
            amount_usd=Decimal("50"),
            idempotency_key="TRX-T1-PURCHASE:RC_UNRECOGNIZED",
            business_created_at=datetime(2026, 6, 18, 6),
        )
    )
    db_session.flush()
    assert runner.real_state(sessions, {CUSTOMER}) == (1, 0)
    assert runner.remove_real_cases(sessions, {"DSP-20261003-000001"}, {"CLI-OTHER"}) == 0
    assert runner.remove_real_cases(sessions, {"DSP-20261003-000001"}, {CUSTOMER}) == 1
    assert runner.real_state(sessions, {CUSTOMER}) == (0, 0)
    assert runner.remove_real_cases(sessions, set(), {CUSTOMER}) == 0


def test_a_smoke_run_names_evaluation_cases_only() -> None:
    settings = Settings(_env_file=None, business_date=date(2026, 6, 17))
    with pytest.raises(HarnessGuardError, match="not evaluation cases"):
        runner.load_cases(settings, ["S001"], runner.Options(only=frozenset({"S999"})))
    with pytest.raises(HarnessGuardError, match="DATABASE_URL"):
        runner.load_cases(settings, ["S001"], runner.Options())
    with pytest.raises(HarnessGuardError, match="BUSINESS_DATE"):
        runner.run_evaluation(
            Settings(_env_file=None, business_date=date(2026, 6, 1)), POLICY, runner.Options()
        )


def test_outputs_are_written(tmp_path: Path) -> None:
    from tests.evaluation.test_harness_metrics import evaluation

    e = evaluation()
    written = runner.write_outputs(e, runner.Options(output=tmp_path, write_docs=False))
    assert [p.name for p in written] == ["results.csv", "m18_evaluation.json"]
    e.partial = True
    assert (
        runner.write_outputs(e, runner.Options(output=tmp_path))[-1].name
        == "m18_evaluation_partial.json"
    )


def test_the_baseline_words_never_carry_an_identifier() -> None:
    from app.evaluation.harness.baseline import scrub

    case = seeded_cases({"S071"})[0]
    words: dict[str, Any] = {
        "outcome": "INFORM",
        "triggered_rules": [],
        "queue": None,
        "priority": None,
        "rationale": "TRX-8YRH1U8OHC6RHVPOX4KZ of CLI-ETG3VM0X7UTD is pending",
    }
    _, rationale = asyncio.run(
        BaselinePredictor(Completer(words), "P").predict(
            case, BaselineRecords(LLMContext(customer_ref="CUS-x"))
        )
    )
    assert rationale == "[ref] of [ref] is pending"
    assert scrub("a SEED-T001-t1 stays") == "a SEED-T001-t1 stays"
