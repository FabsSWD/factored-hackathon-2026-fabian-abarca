from __future__ import annotations

import asyncio
import copy
import json
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest

from app import interfaces
from app.contracts import ModelCall, ModelSignals, ModelSource, ReasonCode
from app.deadline import Deadline
from app.decision.client import KevConfig, KevDecisionClient
from app.decision.questions import load_kev_questions
from tests.decision.conftest import BASE_URL, FakeKev, Ticker, context, fixture, make_client


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def signals(client: KevDecisionClient, message: str = "No reconozco un cargo") -> ModelSignals:
    return run(client.signals(message, context()))


def response(payload: Any, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


REAL_ES = fixture("kev_es_response.json")
REAL_PT = fixture("kev_pt_response.json")


# --- Contract ------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["kev_es.json", "kev_pt.json"])
def test_request_matches_the_recorded_real_requests(client: KevDecisionClient, name: str) -> None:
    recorded = fixture(name)
    assert client.request_body(recorded["state"]) == recorded


def test_request_is_sent_to_systemone_with_the_scrubbed_message_and_no_context(
    client: KevDecisionClient, fake: FakeKev
) -> None:
    fake.responses = [response(REAL_ES)]
    signals(client, "Mi correo es ana@example.test y no reconozco 4111 1111 1111 4821")
    (request,) = fake.requests
    assert request.method == "POST"
    assert str(request.url) == f"{BASE_URL}/v1/systemone"
    body = json.loads(request.content)
    assert set(body) == {"model", "state", "questions"}
    assert body["model"] == "kev-latest"
    assert "ana@example.test" not in body["state"]
    assert "****4821" in body["state"]
    assert "Authorization" not in request.headers
    assert set(body["questions"]) == {"reason_code", "ambiguous", "escalation_risk"}


# --- Real responses ----------------------------------------------------------------------------


def test_real_spanish_response(client: KevDecisionClient, fake: FakeKev) -> None:
    fake.responses = [response(REAL_ES)]
    result = signals(client)
    assert result.source is ModelSource.KEV
    assert result.reason_code_probs == {
        ReasonCode.UNRECOGNIZED: 0.5362,
        ReasonCode.DUPLICATE: 0.0099,
        ReasonCode.INCORRECT_AMOUNT: 0.176,
        ReasonCode.NOT_RECEIVED: 0.0654,
        ReasonCode.FEE: 0.0383,
    }
    assert result.reason_code_other == 0.1741
    assert result.ambiguity == 0.6005
    assert result.escalation_risk == 0.3479
    assert result.manipulation is None
    assert result.top_reason_code == (ReasonCode.UNRECOGNIZED, 0.5362)


def test_real_portuguese_response(client: KevDecisionClient, fake: FakeKev) -> None:
    fake.responses = [response(REAL_PT)]
    result = signals(client, "Fui cobrado duas vezes")
    assert result.top_reason_code == (ReasonCode.DUPLICATE, 0.8169)
    assert result.reason_code_other == 0.1232
    assert (result.ambiguity, result.escalation_risk) == (0.5122, 0.4315)
    total = sum(result.reason_code_probs.values()) + (result.reason_code_other or 0)
    assert 0.99 <= total <= 1.01


def test_model_call_records_latencies_tokens_questions_and_serving_details(
    fake: FakeKev, calls: list[ModelCall]
) -> None:
    ticker = Ticker()

    def slow(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            return response(fixture("kev_models.json"))
        ticker.now += 0.3
        return response(REAL_ES)

    client = KevDecisionClient(
        KevConfig(BASE_URL), load_kev_questions(), httpx.MockTransport(slow), calls.append, ticker
    )
    info = run(client.refresh_model_info())
    result = signals(client)
    assert info == {
        "run": "jaredpalmer/kev-0.8b",
        "release_date": "2026-09-24",
        "temperature": "2.3510958125672174",
        "dtype": "bfloat16",
        "device": "cuda",
    }
    assert result.model_version == "jaredpalmer/kev-0.8b@2026-09-24"
    # Only the run and release date travel with the signals (for the handoff packet).
    assert result.model_info == {"run": "jaredpalmer/kev-0.8b", "release_date": "2026-09-24"}
    (call,) = calls
    questions = load_kev_questions()
    assert call.provider == "kev"
    assert call.model == "kev-latest"
    assert call.response_model == "jaredpalmer/kev-0.8b"
    assert call.prompt_version == "kev_questions@1.0.0"
    assert call.prompt_hash == questions.prompt_hash
    assert call.purpose == "decision_signals"
    assert (call.input_tokens, call.output_tokens) == (167, 163)
    assert call.latency_ms == pytest.approx(300.0)
    assert call.server_latency_ms == 267.9
    assert call.model_info == info
    assert call.success is True


def test_model_version_without_serving_details(
    client: KevDecisionClient, fake: FakeKev, calls: list[ModelCall]
) -> None:
    fake.models = httpx.Response(500)
    fake.responses = [response(REAL_ES)]
    result = signals(client)
    assert result.model_version == "kev-latest"  # the alias, and the failure is recorded
    assert result.model_info == {}
    failure = next(c for c in calls if c.purpose == "model_info")
    assert failure.success is False and "v1/models" in (failure.error or "")


def test_serving_details_are_read_lazily_and_cached(
    client: KevDecisionClient, fake: FakeKev
) -> None:
    assert fake.model_requests == 0  # nothing at construction (startup)
    fake.responses = [response(REAL_ES), response(REAL_ES)]
    first = signals(client)
    assert fake.model_requests == 1
    assert first.model_version == "jaredpalmer/kev-0.8b@2026-09-24"
    second = signals(client)
    assert fake.model_requests == 1  # cached
    assert second.model_version == first.model_version


def test_serving_details_are_not_read_after_a_failed_call(
    client: KevDecisionClient, fake: FakeKev
) -> None:
    fake.responses = [httpx.Response(500)]
    signals(client)
    assert fake.model_requests == 0


def test_failed_serving_details_are_retried_a_bounded_number_of_times(
    client: KevDecisionClient, fake: FakeKev
) -> None:
    from app.decision.client import MODEL_INFO_ATTEMPTS

    fake.models = httpx.Response(503)
    fake.responses = [response(REAL_ES) for _ in range(MODEL_INFO_ATTEMPTS + 2)]
    for _ in range(MODEL_INFO_ATTEMPTS + 2):
        assert signals(client).model_version == "kev-latest"
    assert fake.model_requests == MODEL_INFO_ATTEMPTS


def test_serving_details_wait_when_the_deadline_is_nearly_spent(
    fake: FakeKev, calls: list[ModelCall]
) -> None:
    ticker = Ticker()
    deadline = Deadline(1.0, monotonic=ticker)
    fake.responses = [response(REAL_ES)]

    original = fake.handler

    def slow(request: httpx.Request) -> httpx.Response:
        ticker.now += 0.95  # the Kev call spends almost the whole deadline
        return original(request)

    client = KevDecisionClient(
        KevConfig(BASE_URL, 2.0),
        load_kev_questions(),
        httpx.MockTransport(slow),
        calls.append,
        ticker,
    )
    run(client.signals("Me cobraron dos veces", context(), deadline))
    assert fake.model_requests == 0


# --- Malformed responses -------------------------------------------------------------------------


def mutate(change: Any) -> dict[str, Any]:
    payload = copy.deepcopy(REAL_ES)
    change(payload)
    return payload  # type: ignore[no-any-return]


def _probs(payload: dict[str, Any]) -> dict[str, Any]:
    return payload["answers"]["reason_code"]["probabilities"]  # type: ignore[no-any-return]


MALFORMED: list[tuple[str, Any]] = [
    ("missing class", mutate(lambda p: _probs(p).pop("OTHER"))),
    ("extra class", mutate(lambda p: _probs(p).update({"RC_OTHER": 0.0}))),
    ("sum too low", mutate(lambda p: _probs(p).update({"OTHER": 0.1}))),
    ("sum too high", mutate(lambda p: _probs(p).update({"OTHER": 0.2}))),
    ("NaN", mutate(lambda p: _probs(p).update({"OTHER": float("nan")}))),
    ("negative", mutate(lambda p: _probs(p).update({"RC_FEE": -0.0383}))),
    ("above one", mutate(lambda p: _probs(p).update({"RC_UNRECOGNIZED": 1.5}))),
    ("string", mutate(lambda p: _probs(p).update({"RC_FEE": "0.0383"}))),
    ("boolean", mutate(lambda p: _probs(p).update({"RC_FEE": True}))),
    ("extra top-level field", mutate(lambda p: p.update({"debug": 1}))),
    (
        "extra answer",
        mutate(lambda p: p["answers"].update({"human_requested": {"type": "noul", "noul": 0.5}})),
    ),
    ("missing answer", mutate(lambda p: p["answers"].pop("escalation_risk"))),
    ("extra noul field", mutate(lambda p: p["answers"]["ambiguous"].update({"extra": 1}))),
    ("noul above one", mutate(lambda p: p["answers"]["ambiguous"].update({"noul": 1.2}))),
    ("noul infinite", mutate(lambda p: p["answers"]["ambiguous"].update({"noul": float("inf")}))),
    ("reason field missing", mutate(lambda p: p["answers"]["reason_code"].pop("confidence"))),
    ("answers not an object", mutate(lambda p: p.update({"answers": []}))),
    ("no answers", mutate(lambda p: p.pop("answers"))),
    ("not an object", ["a", "list"]),
]


@pytest.mark.parametrize(("case", "payload"), MALFORMED, ids=[case for case, _ in MALFORMED])
def test_malformed_responses_are_unavailable_and_never_repaired(
    client: KevDecisionClient,
    fake: FakeKev,
    calls: list[ModelCall],
    case: str,
    payload: Any,
) -> None:
    fake.responses = [httpx.Response(200, text=json.dumps(payload))]
    result = signals(client)
    assert result == ModelSignals(source=ModelSource.UNAVAILABLE)
    assert len(fake.requests) == 1  # no retries
    assert calls[0].success is False
    assert calls[0].error is not None and "malformed" in calls[0].error


def test_body_that_is_not_json_is_malformed(
    client: KevDecisionClient, fake: FakeKev, calls: list[ModelCall]
) -> None:
    fake.responses = [httpx.Response(200, text="<html>oops</html>")]
    assert signals(client).source is ModelSource.UNAVAILABLE
    assert calls[0].error == "malformed response"


def test_optional_usage_and_latency_may_be_missing_or_invalid(
    client: KevDecisionClient, fake: FakeKev, calls: list[ModelCall]
) -> None:
    payload = mutate(lambda p: p.update({"usage": {"input_tokens": -1}, "latency_ms": "fast"}))
    fake.responses = [response(payload)]
    assert signals(client).source is ModelSource.KEV
    assert (calls[0].input_tokens, calls[0].output_tokens) == (None, None)
    assert calls[0].server_latency_ms is None


# --- Failures: timeout, 5xx, connection refused -----------------------------------------


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("refused"), "network error"),
        (httpx.Response(500), "HTTP 500"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.Response(404), "HTTP 404"),
    ],
)
def test_failures_return_unavailable_without_retrying(
    client: KevDecisionClient,
    fake: FakeKev,
    calls: list[ModelCall],
    failure: httpx.Response | Exception,
    error: str,
) -> None:
    fake.responses = [failure]
    assert signals(client) == ModelSignals(source=ModelSource.UNAVAILABLE)
    assert len(fake.requests) == 1
    assert calls[0].error == error
    assert calls[0].success is False


def test_unconfigured_client_is_unavailable_without_calls() -> None:
    client = KevDecisionClient(None, load_kev_questions())
    assert not client.configured
    assert run(client.signals("hola", context())).source is ModelSource.UNAVAILABLE
    assert run(client.refresh_model_info()) == {}


# --- Deadline -------------------------------------------------------------------------------


def test_timeout_is_capped_by_the_turn_deadline(fake: FakeKev, calls: list[ModelCall]) -> None:
    ticker = Ticker()
    client = make_client(fake, calls, timeout=2.0, monotonic=ticker)
    deadline = Deadline(1.0, ticker)
    ticker.now += 0.5
    fake.responses = [response(REAL_ES)]
    run(client.signals("hola", context(), deadline))
    assert fake.requests[0].extensions["timeout"]["read"] == pytest.approx(0.5)


def test_no_call_without_time_left(fake: FakeKev, calls: list[ModelCall]) -> None:
    ticker = Ticker()
    client = make_client(fake, calls, monotonic=ticker)
    deadline = Deadline(1.0, ticker)
    ticker.now += 0.95
    assert run(client.signals("hola", context(), deadline)).source is ModelSource.UNAVAILABLE
    assert fake.requests == []


def test_timeout_uses_the_configured_value_without_deadline(
    fake: FakeKev, calls: list[ModelCall]
) -> None:
    fake.responses = [response(REAL_ES)]
    run(make_client(fake, calls, timeout=2.0).signals("hola", context()))
    assert fake.requests[0].extensions["timeout"]["read"] == pytest.approx(2.0)


# --- /v1/models ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "failure",
    [
        httpx.Response(500),
        httpx.Response(200, json={"models": [{"name": "other-model"}]}),
        httpx.Response(200, json={"no": "models"}),
        httpx.ConnectError("down"),
    ],
)
def test_model_info_failures_leave_it_empty(
    client: KevDecisionClient, fake: FakeKev, failure: httpx.Response | Exception
) -> None:
    fake.models = failure
    assert run(client.refresh_model_info()) == {}
    assert client.model_info == {}


# --- Configuration and interface ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("base_url", "timeout", "message"),
    [("", 2.0, "KEV_BASE_URL"), (BASE_URL, 0.0, "KEV_TIMEOUT_SECONDS")],
)
def test_invalid_configuration(base_url: str, timeout: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        KevConfig(base_url, timeout)


def test_implements_the_module_interface(client: KevDecisionClient) -> None:
    assert isinstance(client, interfaces.DecisionClient)


def test_works_without_a_recorder(fake: FakeKev) -> None:
    fake.responses = [response(REAL_ES), httpx.Response(500)]
    client = KevDecisionClient(
        KevConfig(BASE_URL), load_kev_questions(), httpx.MockTransport(fake.handler)
    )
    assert signals(client).source is ModelSource.KEV
    assert signals(client).source is ModelSource.UNAVAILABLE


def test_serving_details_failure_without_a_recorder(fake: FakeKev) -> None:
    fake.models = httpx.Response(500)
    fake.responses = [response(REAL_ES)]
    client = KevDecisionClient(
        KevConfig(BASE_URL, 2.0), load_kev_questions(), httpx.MockTransport(fake.handler)
    )
    assert signals(client).model_version == "kev-latest"


# --- Jev (hosted): the same API with a bearer key and its own model alias ---------------------


def test_jev_sends_the_key_and_its_model_alias_on_every_request(
    fake: FakeKev, calls: list[ModelCall]
) -> None:
    headers: list[str | None] = []
    models = copy.deepcopy(fixture("kev_models.json"))
    models["models"][0]["name"] = "jev-latest"
    models["models"][0]["run"] = "typesafe/jev"

    def handler(request: httpx.Request) -> httpx.Response:
        headers.append(request.headers.get("Authorization"))
        if request.url.path == "/v1/models":
            return response(models)
        return fake.handler(request)

    fake.responses = [response(REAL_ES)]
    client = KevDecisionClient(
        KevConfig(BASE_URL, 2.0, api_key="jev-secret", model="jev-latest"),
        load_kev_questions(),
        httpx.MockTransport(handler),
        calls.append,
    )
    result = signals(client)
    assert result.source is ModelSource.KEV
    assert headers == ["Bearer jev-secret", "Bearer jev-secret"]  # /v1/systemone and /v1/models
    assert json.loads(fake.requests[0].content)["model"] == "jev-latest"
    assert result.model_version.startswith("typesafe/jev@")
    assert calls[0].model == "jev-latest"
    assert "jev-secret" not in repr(KevConfig(BASE_URL, 2.0, api_key="jev-secret"))


def test_without_a_key_or_model_the_request_is_kevs(client: KevDecisionClient) -> None:
    assert client.model == "kev-latest"


def test_jev_traces_name_the_version_the_server_resolved(
    fake: FakeKev, calls: list[ModelCall]
) -> None:
    # Documented Jev shape: /v1/models lists the alias with a release date (no run), no
    # latency_ms in the answer, and the response's model is the versioned id.
    models = {"models": [{"name": "jev-latest", "description": "flagship", "release_date": "x"}]}
    payload = mutate(lambda p: p.update({"model": "jev-1.13.0"}))
    del payload["latency_ms"]

    def handler(request: httpx.Request) -> httpx.Response:
        return response(models if request.url.path == "/v1/models" else payload)

    client = KevDecisionClient(
        KevConfig(BASE_URL, 2.0, api_key="k", model="jev-latest"),
        load_kev_questions(),
        httpx.MockTransport(handler),
        calls.append,
    )
    result = signals(client)
    assert result.source is ModelSource.KEV
    assert result.model_version == "jev-1.13.0"
    (call,) = calls
    assert (call.model, call.response_model) == ("jev-latest", "jev-1.13.0")
    assert call.server_latency_ms is None
