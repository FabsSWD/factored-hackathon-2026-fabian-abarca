from __future__ import annotations

import asyncio
import random
import re
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.contracts import ModelCall
from app.deadline import Deadline
from app.llm_adapter.client import (
    MIN_ATTEMPT_SECONDS,
    JsonCompletion,
    LLMClientConfig,
    LLMError,
    LLMRefusalError,
    OpenAIJsonClient,
    prompt_hash,
)
from app.llm_adapter.factory import adapter_from_settings
from app.settings import Settings
from tests.llm_adapter.conftest import API_KEY, MODEL, FakeOpenAI, completion, make_client


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


class FakeClock:
    """Monotonic clock that moves only when a request takes time or the client sleeps."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def timed_client(
    handler: Any,
    clock: FakeClock,
    calls: list[ModelCall] | None = None,
    **config: Any,
) -> OpenAIJsonClient:
    values: dict[str, Any] = {"api_key": API_KEY, "model": MODEL, **config}
    return OpenAIJsonClient(
        LLMClientConfig(**values),
        httpx.MockTransport(handler),
        (calls if calls is not None else []).append,
        monotonic=clock,
        sleep=clock.sleep,
        rng=random.Random(7),
    )


def ask(client: OpenAIJsonClient, **overrides: Any) -> JsonCompletion:
    arguments: dict[str, Any] = {
        "purpose": "extract_slots",
        "prompt_version": "extract@test",
        "system": "system prompt",
        "user": "user message",
        "schema_name": "test_schema",
        "schema": {"type": "object"},
        "max_output_tokens": 50,
    }
    arguments.update(overrides)
    return run(client.complete_json(**arguments))


# --- Success and traceability --------------------------------------------------------------


def test_success_records_model_version_prompt_tokens_and_latency() -> None:
    clock = FakeClock()

    def slow(request: httpx.Request) -> httpx.Response:
        clock.now += 0.25
        payload = completion({"ok": True}, prompt_tokens=321, completion_tokens=12).json()
        payload |= {"model": "gpt-6-luna-2026-09-01", "system_fingerprint": "fp_abc"}
        return httpx.Response(200, json=payload)

    calls: list[ModelCall] = []
    result = ask(timed_client(slow, clock, calls))
    assert result.data == result.value == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (321, 12)
    (call,) = calls
    assert call == ModelCall(
        provider="openai",
        model=MODEL,
        response_model="gpt-6-luna-2026-09-01",
        system_fingerprint="fp_abc",
        prompt_version="extract@test",
        prompt_hash=prompt_hash("system prompt", {"type": "object"}),
        purpose="extract_slots",
        input_tokens=321,
        output_tokens=12,
        latency_ms=250.0,
        success=True,
        error=None,
    )


def test_prompt_hash_tracks_prompt_and_schema() -> None:
    base = prompt_hash("system", {"a": 1})
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", base)
    assert prompt_hash("system", {"a": 1}) == base
    assert prompt_hash("system!", {"a": 1}) != base
    assert prompt_hash("system", {"a": 2}) != base


def test_request_uses_strict_json_schema_and_bearer_key(fake: FakeOpenAI) -> None:
    fake.responses = [completion({"ok": True})]
    ask(make_client(fake, []), schema={"type": "object", "properties": {}})
    (request,) = fake.requests
    assert request.url.path.endswith("/chat/completions")
    assert request.headers["Authorization"] == f"Bearer {API_KEY}"
    (body,) = fake.bodies()
    assert body["model"] == MODEL
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert body["response_format"]["json_schema"]["name"] == "test_schema"
    assert body["max_completion_tokens"] == 50
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert API_KEY not in request.content.decode()


def test_validator_value_is_returned(fake: FakeOpenAI) -> None:
    fake.responses = [completion({"n": 2})]
    result = ask(make_client(fake, []), validate=lambda data: data["n"] * 10)
    assert result.value == 20


def test_response_sink_receives_raw_answers_of_successful_calls(fake: FakeOpenAI) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []
    fake.responses = [completion("not json"), completion({"raw": 1})]
    config = LLMClientConfig(api_key=API_KEY, model=MODEL, retry_wait_seconds=0)
    client = OpenAIJsonClient(
        config, httpx.MockTransport(fake.handler), response_sink=lambda p, d: seen.append((p, d))
    )
    ask(client)
    assert seen == [("extract_slots", {"raw": 1})]


# --- Bounded retries ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("failure", "error"),
    [
        (completion("not json at all"), "invalid output"),
        (completion('["a", "list"]'), "the answer is not a JSON object"),
        (httpx.Response(200, json={"no": "choices"}), "invalid output"),
        (httpx.Response(200, text="<html>gateway</html>"), "invalid output"),
        (httpx.Response(429, json={"error": "rate"}), "HTTP 429"),
        (httpx.Response(500, json={"error": "boom"}), "HTTP 500"),
        (httpx.Response(503), "HTTP 503"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ConnectError("down"), "network error"),
    ],
    ids=[
        "invalid-json",
        "not-object",
        "no-choices",
        "html",
        "429",
        "500",
        "503",
        "timeout",
        "network",
    ],
)
def test_retryable_failures_are_bounded_then_controlled(
    fake: FakeOpenAI, calls: list[ModelCall], failure: httpx.Response | Exception, error: str
) -> None:
    fake.responses = [failure, failure, failure]
    with pytest.raises(LLMError, match="no usable answer within the limits"):
        ask(make_client(fake, calls, max_retries=2))
    assert len(fake.requests) == 3
    assert [call.success for call in calls] == [False, False, False]
    assert {call.error for call in calls} == {error}


def test_retry_then_success(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(429), completion("{broken"), completion({"ok": 1})]
    assert ask(make_client(fake, calls)).data == {"ok": 1}
    assert [call.success for call in calls] == [False, False, True]


def test_validator_rejection_is_retried(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [completion({"n": -1}), completion({"n": 3})]

    def positive(data: dict[str, Any]) -> int:
        if data["n"] < 0:
            raise ValueError("negative")
        return int(data["n"])

    assert ask(make_client(fake, calls), validate=positive).value == 3
    assert calls[0].error == "invalid output"


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_errors_are_not_retried(
    fake: FakeOpenAI, calls: list[ModelCall], status: int
) -> None:
    fake.responses = [httpx.Response(status, json={"error": "bad"})]
    with pytest.raises(LLMError, match=f"HTTP {status}"):
        ask(make_client(fake, calls))
    assert len(fake.requests) == 1
    assert calls[0].error == f"HTTP {status}"


def test_refusal_is_not_retried(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [
        httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"role": "assistant", "content": None, "refusal": "I can't help"}}
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            },
        )
    ]
    with pytest.raises(LLMRefusalError):
        ask(make_client(fake, calls))
    assert len(fake.requests) == 1
    assert (calls[0].input_tokens, calls[0].output_tokens) == (10, 3)


def test_zero_retries_means_one_attempt(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(500)]
    with pytest.raises(LLMError):
        ask(make_client(fake, calls, max_retries=0))
    assert len(fake.requests) == 1


# --- Turn deadline, backoff and Retry-After ----------------------------------------------------


def test_three_timeouts_never_exceed_the_turn_deadline() -> None:
    clock = FakeClock()

    def times_out(request: httpx.Request) -> httpx.Response:
        clock.now += request.extensions["timeout"]["read"]  # the attempt uses its full timeout
        raise httpx.ReadTimeout("slow")

    calls: list[ModelCall] = []
    client = timed_client(
        times_out, clock, calls, timeout_seconds=8.0, turn_deadline_seconds=20.0, max_retries=2
    )
    started = clock.now
    with pytest.raises(LLMError):
        ask(client)
    assert clock.now - started <= 20.0
    assert len(calls) == 3
    assert all(call.error == "timeout" for call in calls)


def test_attempt_timeout_is_capped_by_the_remaining_deadline(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    fake.responses = [completion({"ok": True})]
    client = timed_client(fake.handler, clock, timeout_seconds=20.0)
    deadline = Deadline(5.0, clock)
    clock.now += 2.0
    ask(client, deadline=deadline)
    assert fake.requests[0].extensions["timeout"]["read"] == pytest.approx(3.0)


def test_no_attempt_starts_without_time_left(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    deadline = Deadline(MIN_ATTEMPT_SECONDS / 2, clock)
    with pytest.raises(LLMError, match="no time left"):
        ask(timed_client(fake.handler, clock), deadline=deadline)
    assert fake.requests == []


def test_no_retry_starts_if_it_does_not_fit() -> None:
    clock = FakeClock()

    def slow_failure(request: httpx.Request) -> httpx.Response:
        clock.now += 9.5
        return httpx.Response(500)

    client = timed_client(slow_failure, clock, turn_deadline_seconds=10.0, retry_wait_seconds=0.5)
    with pytest.raises(LLMError):
        ask(client)
    assert clock.sleeps == []  # 0.5 s left: not enough for a wait plus a minimal attempt


def test_backoff_is_exponential_with_jitter(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    fake.responses = [httpx.Response(500)] * 4
    client = timed_client(fake.handler, clock, max_retries=3, retry_wait_seconds=1.0)
    with pytest.raises(LLMError):
        ask(client)
    assert len(clock.sleeps) == 3
    for attempt, wait in enumerate(clock.sleeps):
        assert 0.0 <= wait <= 1.0 * 2**attempt
    assert len(set(clock.sleeps)) == 3  # jittered, not constant


def test_retry_after_is_honoured_when_it_fits(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    fake.responses = [httpx.Response(429, headers={"Retry-After": "3"}), completion({"ok": 1})]
    assert ask(timed_client(fake.handler, clock)).data == {"ok": 1}
    assert clock.sleeps == [3.0]


def test_retry_after_longer_than_the_deadline_stops_retrying(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    fake.responses = [httpx.Response(429, headers={"Retry-After": "30"})]
    with pytest.raises(LLMError, match="HTTP 429"):
        ask(timed_client(fake.handler, clock, turn_deadline_seconds=20.0))
    assert len(fake.requests) == 1
    assert clock.sleeps == []


def test_retry_after_as_a_date_falls_back_to_backoff(fake: FakeOpenAI) -> None:
    clock = FakeClock()
    fake.responses = [
        httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}),
        completion({"ok": 1}),
    ]
    ask(timed_client(fake.handler, clock, retry_wait_seconds=0.5))
    assert len(clock.sleeps) == 1
    assert 0.0 <= clock.sleeps[0] <= 0.5


# --- Safety and configuration ----------------------------------------------------------------


def test_errors_never_contain_the_api_key(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(401)]
    with pytest.raises(LLMError) as info:
        ask(make_client(fake, calls))
    assert API_KEY not in str(info.value)
    assert all(API_KEY not in (call.error or "") for call in calls)


def test_missing_usage_and_model_are_recorded_as_none(
    fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [
        httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    ]
    ask(make_client(fake, calls))
    call = calls[0]
    assert (call.input_tokens, call.output_tokens) == (None, None)
    assert (call.response_model, call.system_fingerprint) == (None, None)


def test_works_without_a_recorder(fake: FakeOpenAI) -> None:
    fake.responses = [completion({"ok": True})]
    config = LLMClientConfig(api_key=API_KEY, model=MODEL)
    client = OpenAIJsonClient(config, httpx.MockTransport(fake.handler))
    assert client.model == MODEL
    assert ask(client).data == {"ok": True}


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"api_key": ""}, "OPENAI_API_KEY"),
        ({"model": ""}, "LLM_MODEL"),
        ({"max_retries": -1}, "max_retries"),
        ({"turn_deadline_seconds": 0}, "turn_deadline_seconds"),
    ],
)
def test_invalid_configuration(overrides: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {"api_key": API_KEY, "model": MODEL, **overrides}
    with pytest.raises(ValueError, match=message):
        LLMClientConfig(**values)


def test_deadline() -> None:
    clock = FakeClock()
    deadline = Deadline(5.0, clock)
    assert deadline.remaining() == 5.0
    assert not deadline.expired()
    clock.now += 7.0
    assert deadline.remaining() == 0.0
    assert deadline.expired()
    with pytest.raises(ValueError, match="positive"):
        Deadline(0)


def test_adapter_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        business_date="2026-06-17",  # type: ignore[arg-type]
        openai_api_key=SecretStr(API_KEY),
        llm_model=MODEL,
        llm_max_retries=1,
        llm_turn_deadline_seconds=12.0,
        llm_connect_enabled=False,
    )
    adapter = adapter_from_settings(settings)
    deadline = adapter.new_deadline()
    assert 11.0 < deadline.remaining() <= 12.0
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        adapter_from_settings(
            Settings(_env_file=None, business_date="2026-06-17", llm_model=MODEL)  # type: ignore[arg-type]
        )


def test_cached_input_tokens_are_recorded_when_reported(
    fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    payload = completion({"ok": True}, prompt_tokens=1340, completion_tokens=90).json()
    payload["usage"]["prompt_tokens_details"] = {"cached_tokens": 1024}
    fake.responses = [httpx.Response(200, json=payload)]
    ask(make_client(fake, calls))
    assert (calls[0].input_tokens, calls[0].cached_input_tokens) == (1340, 1024)


def test_cached_input_tokens_unknown_when_not_reported(
    fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [completion({"ok": True})]
    ask(make_client(fake, calls))
    assert calls[0].cached_input_tokens is None
