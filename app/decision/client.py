"""Decision Client (architecture §3): Kev's typed probabilities through its TypeSafe API.

- One ``POST /v1/systemone`` per message, with the questions of ``config/kev_questions.yaml``
  and the customer's message scrubbed like for the LLM (``scrub_message``), without context.
- Short timeout (``KEV_TIMEOUT_SECONDS``, capped by what is left of the turn deadline) and no
  retries: Kev runs in parallel with ``extract``, and the fallback covers its failures.
- The answer is validated strictly and never repaired: the six reason choices present, each a
  finite probability in [0, 1], summing to 1 within ±0.01; each ``noul`` in [0, 1]; no field
  missing or extra. Anything else counts as malformed.
- It never raises: on any failure it returns ``source=unavailable`` signals, and
  ``app.decision.fallback.resolve_signals`` applies the fallback.
- Every call is a ModelCall: client and server latency, tokens, questions version and hash,
  and the serving details read from ``GET /v1/models`` (``refresh_model_info``).

The signals are informative only; this module decides nothing.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from app.contracts import (
    PROBABILITY_SUM_TOLERANCE,
    LLMContext,
    ModelCall,
    ModelSignals,
    ModelSource,
    ReasonCode,
)
from app.deadline import Deadline
from app.decision.questions import OTHER, QUESTION_TYPES, REASON_CHOICES, KevQuestions
from app.llm_adapter.minimization import scrub_message

PROVIDER = "kev"
PURPOSE = "decision_signals"
MIN_CALL_SECONDS = 0.1
MODEL_INFO_FIELDS = ("run", "release_date", "temperature", "dtype", "device")

CallRecorder = Callable[[ModelCall], None]


class KevResponseError(ValueError):
    """Kev answered outside the contract."""


@dataclass(frozen=True)
class KevConfig:
    base_url: str
    timeout_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.base_url:
            raise ValueError("KEV_BASE_URL is not set")
        if self.timeout_seconds <= 0:
            raise ValueError("KEV_TIMEOUT_SECONDS must be positive")


@dataclass(frozen=True)
class _Parsed:
    signals: ModelSignals
    input_tokens: int | None
    output_tokens: int | None
    server_latency_ms: float | None


class KevDecisionClient:
    def __init__(
        self,
        config: KevConfig | None,
        questions: KevQuestions,
        transport: httpx.AsyncBaseTransport | None = None,
        recorder: CallRecorder | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._questions = questions
        self._transport = transport
        self._recorder = recorder
        self._monotonic = monotonic
        self._model_info: dict[str, str] = {}

    @property
    def configured(self) -> bool:
        return self._config is not None

    @property
    def model_info(self) -> dict[str, str]:
        return dict(self._model_info)

    def request_body(self, message: str) -> dict[str, Any]:
        """The exact request sent to Kev (pinned by the contract test)."""
        return {
            "model": self._questions.model,
            "state": scrub_message(message),
            "questions": self._questions.questions,
        }

    async def refresh_model_info(self) -> dict[str, str]:
        """Read the serving details from ``GET /v1/models``; empty when Kev is unreachable."""
        if self._config is None:
            return {}
        try:
            async with self._http(self._config.timeout_seconds) as http:
                response = await http.get("/v1/models")
            response.raise_for_status()
            models = response.json()["models"]
            entry = next(m for m in models if m.get("name") == self._questions.model)
            self._model_info = {
                field: str(entry[field]) for field in MODEL_INFO_FIELDS if field in entry
            }
        except (httpx.HTTPError, ValueError, KeyError, TypeError, StopIteration):
            return {}
        return self.model_info

    async def signals(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ModelSignals:
        if self._config is None:
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        budget = self._config.timeout_seconds
        if deadline is not None:
            budget = min(budget, deadline.remaining())
        if budget < MIN_CALL_SECONDS:
            return ModelSignals(source=ModelSource.UNAVAILABLE)

        started = self._monotonic()
        try:
            async with self._http(budget) as http:
                response = await http.post("/v1/systemone", json=self.request_body(message))
            if response.status_code >= 400:
                raise KevResponseError(f"HTTP {response.status_code}")
            parsed = self._parse(response.json())
        except httpx.TimeoutException:
            self._record(started, None, "timeout")
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        except httpx.HTTPError:
            self._record(started, None, "network error")
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        except KevResponseError as exc:
            self._record(started, None, str(exc))
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        except (ValueError, KeyError, TypeError):
            self._record(started, None, "malformed response")
            return ModelSignals(source=ModelSource.UNAVAILABLE)
        self._record(started, parsed, None)
        return parsed.signals

    # --- Internals ------------------------------------------------------------------

    def _http(self, timeout: float) -> httpx.AsyncClient:
        assert self._config is not None
        return httpx.AsyncClient(
            base_url=self._config.base_url, timeout=timeout, transport=self._transport
        )

    def _parse(self, payload: Any) -> _Parsed:
        if not isinstance(payload, dict):
            raise KevResponseError("malformed response: not an object")
        unexpected = set(payload) - {"model", "answers", "usage", "latency_ms"}
        if unexpected or "answers" not in payload:
            raise KevResponseError("malformed response: unexpected or missing fields")
        answers = payload["answers"]
        if not isinstance(answers, dict) or set(answers) != set(QUESTION_TYPES):
            raise KevResponseError("malformed response: answers do not match the questions")

        reason = answers["reason_code"]
        if not isinstance(reason, dict) or set(reason) != {
            "type", "choice", "confidence", "probabilities",
        }:  # fmt: skip
            raise KevResponseError("malformed response: reason_code fields")
        probabilities = reason["probabilities"]
        if not isinstance(probabilities, dict) or set(probabilities) != REASON_CHOICES:
            raise KevResponseError("malformed response: reason_code needs the six choices")
        values = {choice: _probability(probabilities[choice]) for choice in REASON_CHOICES}
        total = sum(values.values())
        if abs(total - 1.0) > PROBABILITY_SUM_TOLERANCE:
            raise KevResponseError(f"malformed response: probabilities sum to {total:.4f}")

        nouls = {}
        for name in ("ambiguous", "escalation_risk"):
            answer = answers[name]
            if not isinstance(answer, dict) or set(answer) != {"type", "noul"}:
                raise KevResponseError(f"malformed response: {name} fields")
            nouls[name] = _probability(answer["noul"])

        usage = payload.get("usage") or {}
        latency = payload.get("latency_ms")
        return _Parsed(
            signals=ModelSignals(
                source=ModelSource.KEV,
                model_version=self._model_version(),
                model_info={
                    key: value
                    for key, value in self._model_info.items()
                    if key in ("run", "release_date") and value
                },
                reason_code_probs={ReasonCode(code): values[code.value] for code in ReasonCode},
                reason_code_other=values[OTHER],
                ambiguity=nouls["ambiguous"],
                escalation_risk=nouls["escalation_risk"],
            ),
            input_tokens=_count(usage.get("input_tokens")),
            output_tokens=_count(usage.get("output_tokens")),
            server_latency_ms=_latency(latency),
        )

    def _model_version(self) -> str:
        run = self._model_info.get("run")
        release = self._model_info.get("release_date")
        if run and release:
            return f"{run}@{release}"
        return self._questions.model

    def _record(self, started: float, parsed: _Parsed | None, error: str | None) -> None:
        if self._recorder is None:
            return
        self._recorder(
            ModelCall(
                provider=PROVIDER,
                model=self._questions.model,
                response_model=self._model_info.get("run"),
                prompt_version=self._questions.prompt_version,
                prompt_hash=self._questions.prompt_hash,
                purpose=PURPOSE,
                input_tokens=parsed.input_tokens if parsed else None,
                output_tokens=parsed.output_tokens if parsed else None,
                latency_ms=max(0.0, (self._monotonic() - started) * 1000),
                server_latency_ms=parsed.server_latency_ms if parsed else None,
                model_info=self.model_info,
                success=error is None,
                error=error,
            )
        )


def _probability(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise KevResponseError("malformed response: a probability is not a number")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise KevResponseError("malformed response: a probability is outside [0, 1]")
    return number


def _count(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _latency(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None
