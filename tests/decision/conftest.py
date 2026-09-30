"""A simulated Kev server: every test runs on httpx.MockTransport, never the real Kev."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.contracts import Language, LLMContext, ModelCall
from app.decision.client import KevConfig, KevDecisionClient
from app.decision.questions import load_kev_questions

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "kev"
BASE_URL = "http://kev.test:8008"


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


@dataclass
class FakeKev:
    responses: list[httpx.Response | Exception] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request to Kev")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class Ticker:
    def __init__(self) -> None:
        self.now = 50.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def fake() -> FakeKev:
    return FakeKev()


@pytest.fixture
def calls() -> list[ModelCall]:
    return []


def make_client(
    fake: FakeKev,
    calls: list[ModelCall],
    timeout: float = 2.0,
    monotonic: Ticker | None = None,
) -> KevDecisionClient:
    return KevDecisionClient(
        KevConfig(BASE_URL, timeout),
        load_kev_questions(),
        transport=httpx.MockTransport(fake.handler),
        recorder=calls.append,
        monotonic=monotonic or Ticker(),
    )


@pytest.fixture
def client(fake: FakeKev, calls: list[ModelCall]) -> KevDecisionClient:
    return make_client(fake, calls)


def context(language: Language = Language.ES) -> LLMContext:
    return LLMContext(customer_ref="CUS-pseudonym-7f3a", language=language)
