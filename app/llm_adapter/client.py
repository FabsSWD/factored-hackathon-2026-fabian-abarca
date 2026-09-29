"""Minimal OpenAI Chat Completions client with strict JSON Schema output.

Isolated so that changing provider is a configuration change (architecture §3). Uses httpx
directly; tests inject an ``httpx.MockTransport``, so no test ever reaches the real API.

- One turn deadline bounds everything: each attempt's timeout is capped by what is left, and
  no retry starts unless its wait plus a minimal attempt still fits.
- Bounded retries (tenacity) for timeouts, network errors, 429, 5xx, and answers that are not
  valid JSON or that the caller's validator rejects (values outside the schema). Waits use
  exponential backoff with full jitter; a 429 ``Retry-After`` is honoured when it fits.
  Other 4xx errors and refusals are not retried.
- Every attempt is reported as a ``ModelCall``: requested and reported model, system
  fingerprint, prompt version and hash, tokens, latency, success. The API key never appears in
  errors or records.
- After the last attempt a controlled ``LLMError`` is raised; httpx errors never escape.

Without ``temperature`` the answers can vary between runs: M18 measures variability with at
least three runs per case.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from tenacity import AsyncRetrying, RetryCallState, RetryError, retry_if_exception_type

from app.contracts import ModelCall
from app.deadline import Deadline

PROVIDER = "openai"
MIN_ATTEMPT_SECONDS = 1.0
"""An attempt is only started (or retried) with at least this much time left."""
MAX_BACKOFF_SECONDS = 8.0

CallRecorder = Callable[[ModelCall], None]
ResponseSink = Callable[[str, dict[str, Any]], None]
"""Receives (purpose, raw JSON answer) of successful calls; used to record parser fixtures."""
Sleep = Callable[[float], Awaitable[None]]


class LLMError(Exception):
    """The language model could not produce a usable answer (after bounded retries)."""


class _RetryableError(LLMError):
    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class LLMRefusalError(LLMError):
    """The model refused to answer; retrying the same request would not help."""


@dataclass(frozen=True)
class LLMClientConfig:
    api_key: str
    model: str
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 20.0
    max_retries: int = 2
    retry_wait_seconds: float = 0.5
    turn_deadline_seconds: float = 20.0

    def __post_init__(self) -> None:
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY is not set")
        if not self.model:
            raise ValueError("LLM_MODEL is not set")
        if self.max_retries < 0:
            raise ValueError("max_retries must not be negative")
        if self.turn_deadline_seconds <= 0:
            raise ValueError("turn_deadline_seconds must be positive")


@dataclass(frozen=True)
class JsonCompletion:
    data: dict[str, Any]
    value: Any
    """What the validator returned (``data`` itself without a validator)."""
    input_tokens: int | None
    output_tokens: int | None


Validator = Callable[[dict[str, Any]], Any]
"""Turns the JSON answer into a typed value; raises ValueError when it is out of schema."""


def prompt_hash(system: str, schema: dict[str, Any]) -> str:
    digest = hashlib.sha256(
        (system + "\n" + json.dumps(schema, sort_keys=True)).encode("utf-8")
    ).hexdigest()
    return f"sha256:{digest[:16]}"


@dataclass
class _Attempt:
    purpose: str
    prompt_version: str
    prompt_hash: str
    started: float
    tokens_in: int | None = None
    tokens_out: int | None = None
    response_model: str | None = None
    fingerprint: str | None = None


class OpenAIJsonClient:
    def __init__(
        self,
        config: LLMClientConfig,
        transport: httpx.AsyncBaseTransport | None = None,
        recorder: CallRecorder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Sleep = asyncio.sleep,
        rng: random.Random | None = None,
        response_sink: ResponseSink | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._recorder = recorder
        self._monotonic = monotonic
        self._sleep = sleep
        self._rng = rng or random.Random()
        self._response_sink = response_sink

    @property
    def model(self) -> str:
        return self._config.model

    def new_deadline(self) -> Deadline:
        return Deadline(self._config.turn_deadline_seconds, self._monotonic)

    async def complete_json(
        self,
        *,
        purpose: str,
        prompt_version: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        validate: Validator | None = None,
        deadline: Deadline | None = None,
    ) -> JsonCompletion:
        budget = deadline or self.new_deadline()
        if budget.remaining() < MIN_ATTEMPT_SECONDS:
            raise LLMError(f"{purpose}: no time left in the turn deadline")
        body = {
            "model": self._config.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            "max_completion_tokens": max_output_tokens,
        }
        fingerprint = prompt_hash(system, schema)
        planned_waits: dict[int, float] = {}

        def stop(state: RetryCallState) -> bool:
            if state.attempt_number > self._config.max_retries:
                return True
            wait = self._next_wait(state)
            if budget.remaining() - wait < MIN_ATTEMPT_SECONDS:
                return True
            planned_waits[state.attempt_number] = wait
            return False

        def wait(state: RetryCallState) -> float:
            return planned_waits.get(state.attempt_number, 0.0)

        retrying = AsyncRetrying(
            stop=stop,
            wait=wait,
            retry=retry_if_exception_type(_RetryableError),
            sleep=self._sleep,
            reraise=False,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    record = _Attempt(purpose, prompt_version, fingerprint, self._monotonic())
                    return await self._attempt(body, record, validate, budget)
        except RetryError as exc:
            last = exc.last_attempt.exception()
            raise LLMError(f"{purpose}: no usable answer within the limits ({last})") from None
        raise LLMError(f"{purpose}: no attempt was made")  # pragma: no cover

    def _next_wait(self, state: RetryCallState) -> float:
        error = state.outcome.exception() if state.outcome else None
        if isinstance(error, _RetryableError) and error.retry_after is not None:
            return error.retry_after
        exponent = state.attempt_number - 1
        ceiling = min(MAX_BACKOFF_SECONDS, self._config.retry_wait_seconds * 2**exponent)
        return self._rng.uniform(0.0, ceiling)  # full jitter

    async def _attempt(
        self,
        body: dict[str, Any],
        record: _Attempt,
        validate: Validator | None,
        budget: Deadline,
    ) -> JsonCompletion:
        timeout = max(0.001, min(self._config.timeout_seconds, budget.remaining()))
        try:
            async with httpx.AsyncClient(
                base_url=self._config.base_url, timeout=timeout, transport=self._transport
            ) as http:
                response = await http.post(
                    "/chat/completions",
                    json=body,
                    headers={"Authorization": f"Bearer {self._config.api_key}"},
                )
            if response.status_code == 429:
                raise _RetryableError("HTTP 429", _retry_after(response))
            if response.status_code >= 500:
                raise _RetryableError(f"HTTP {response.status_code}")
            if response.status_code >= 400:
                raise LLMError(f"HTTP {response.status_code}")
            payload = response.json()
            usage = payload.get("usage") or {}
            record.tokens_in = usage.get("prompt_tokens")
            record.tokens_out = usage.get("completion_tokens")
            record.response_model = payload.get("model") or None
            record.fingerprint = payload.get("system_fingerprint") or None
            message = payload["choices"][0]["message"]
            if message.get("refusal"):
                raise LLMRefusalError("the model refused to answer")
            data = json.loads(message.get("content") or "")
            if not isinstance(data, dict):
                raise _RetryableError("the answer is not a JSON object")
            value = validate(data) if validate is not None else data
        except httpx.TimeoutException as exc:
            self._record(record, "timeout")
            raise _RetryableError("timeout") from exc
        except httpx.HTTPError as exc:
            self._record(record, "network error")
            raise _RetryableError("network error") from exc
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # json.JSONDecodeError and pydantic's ValidationError are ValueErrors.
            self._record(record, "invalid output")
            raise _RetryableError("invalid output") from exc
        except LLMError as exc:
            self._record(record, str(exc))
            raise
        self._record(record, None)
        if self._response_sink is not None:
            self._response_sink(record.purpose, data)
        return JsonCompletion(data, value, record.tokens_in, record.tokens_out)

    def _record(self, record: _Attempt, error: str | None) -> None:
        if self._recorder is None:
            return
        self._recorder(
            ModelCall(
                provider=PROVIDER,
                model=self._config.model,
                response_model=record.response_model,
                system_fingerprint=record.fingerprint,
                prompt_version=record.prompt_version,
                prompt_hash=record.prompt_hash,
                purpose=record.purpose,
                input_tokens=record.tokens_in,
                output_tokens=record.tokens_out,
                latency_ms=max(0.0, (self._monotonic() - record.started) * 1000),
                success=error is None,
                error=error,
            )
        )


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None  # HTTP-date form: fall back to the backoff
