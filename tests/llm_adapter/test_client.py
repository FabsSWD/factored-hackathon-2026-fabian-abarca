from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from app.contracts import ModelCall
from app.llm_adapter.client import (
    JsonCompletion,
    LLMClientConfig,
    LLMError,
    LLMRefusalError,
    OpenAIJsonClient,
)
from app.llm_adapter.factory import adapter_from_settings
from app.settings import Settings
from tests.llm_adapter.conftest import API_KEY, MODEL, FakeOpenAI, completion, make_client


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


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


def test_success_records_model_prompt_tokens_and_latency(fake: FakeOpenAI) -> None:
    fake.responses = [completion({"ok": True}, prompt_tokens=321, completion_tokens=12)]
    calls: list[ModelCall] = []
    ticks = iter([10.0, 10.25])
    config = LLMClientConfig(api_key=API_KEY, model=MODEL, retry_wait_seconds=0)
    client = OpenAIJsonClient(
        config, httpx.MockTransport(fake.handler), calls.append, monotonic=lambda: next(ticks)
    )
    result = ask(client)
    assert result.data == {"ok": True}
    assert result.value == {"ok": True}
    assert (result.input_tokens, result.output_tokens) == (321, 12)
    (call,) = calls
    assert call == ModelCall(
        provider="openai",
        model=MODEL,
        prompt_version="extract@test",
        purpose="extract_slots",
        input_tokens=321,
        output_tokens=12,
        latency_ms=250.0,
        success=True,
        error=None,
    )


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
    with pytest.raises(LLMError, match="no usable answer after retries"):
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


def test_errors_never_contain_the_api_key(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(401)]
    with pytest.raises(LLMError) as info:
        ask(make_client(fake, calls))
    assert API_KEY not in str(info.value)
    assert all(API_KEY not in (call.error or "") for call in calls)


def test_missing_usage_is_recorded_as_none(fake: FakeOpenAI, calls: list[ModelCall]) -> None:
    fake.responses = [
        httpx.Response(200, json={"choices": [{"message": {"content": '{"ok": true}'}}]})
    ]
    ask(make_client(fake, calls))
    assert (calls[0].input_tokens, calls[0].output_tokens) == (None, None)


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
    ],
)
def test_invalid_configuration(overrides: dict[str, Any], message: str) -> None:
    values: dict[str, Any] = {"api_key": API_KEY, "model": MODEL, **overrides}
    with pytest.raises(ValueError, match=message):
        LLMClientConfig(**values)


def test_adapter_from_settings() -> None:
    settings = Settings(
        _env_file=None,
        business_date="2026-06-17",  # type: ignore[arg-type]
        openai_api_key=SecretStr(API_KEY),
        llm_model=MODEL,
        llm_max_retries=1,
    )
    assert adapter_from_settings(settings) is not None
    with pytest.raises(ValueError, match="OPENAI_API_KEY"):
        adapter_from_settings(
            Settings(_env_file=None, business_date="2026-06-17", llm_model=MODEL)  # type: ignore[arg-type]
        )
