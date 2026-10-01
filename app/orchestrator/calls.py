"""Model calls of the current turn, for its trace (M11).

The LLM and Kev clients are built once with a recorder; the recorder appends to the list of
the turn running in the current context, so concurrent turns never mix their calls.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from app.contracts import ModelCall

_CURRENT: ContextVar[list[ModelCall] | None] = ContextVar("model_calls", default=None)


def record_call(call: ModelCall) -> None:
    """Recorder for ``OpenAIJsonClient`` and ``KevDecisionClient``."""
    calls = _CURRENT.get()
    if calls is not None:
        calls.append(call)


@contextmanager
def collect_calls() -> Iterator[list[ModelCall]]:
    calls: list[ModelCall] = []
    token = _CURRENT.set(calls)
    try:
        yield calls
    finally:
        _CURRENT.reset(token)
