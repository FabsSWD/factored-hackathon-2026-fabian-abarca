"""A simulated OpenAI API: every test runs against httpx.MockTransport, never the real API."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.contracts import Language, LLMContext, LLMTransaction, ModelCall, SlotName
from app.llm_adapter.adapter import OpenAILLMAdapter
from app.llm_adapter.client import LLMClientConfig, OpenAIJsonClient

API_KEY = "sk-test-not-a-real-key-000000"
MODEL = "gpt-6-luna"

Responder = Callable[[httpx.Request], httpx.Response]


def completion(
    content: dict[str, Any] | str, prompt_tokens: int = 120, completion_tokens: int = 40
) -> httpx.Response:
    body = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(
        200,
        json={
            "id": "chatcmpl-test",
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": body},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        },
    )


def extraction(**overrides: Any) -> dict[str, Any]:
    slots = {
        "transaction_ref": None,
        "reason_code": None,
        "card_in_possession": None,
        "shared_credentials": None,
        "expected_amount": None,
        "expected_delivery_date": None,
        "merchant_contacted": None,
        "fee_ref": None,
        "confirmation": None,
    }
    slots.update(overrides.pop("slots", {}))
    flags = {
        "human_requested": False,
        "account_takeover_reported": False,
        "legal_or_vulnerability": False,
        "authentication_declined": False,
    }
    flags.update(overrides.pop("flags", {}))
    data: dict[str, Any] = {
        "detected_language": "es",
        "language_ambiguous": False,
        "slots": slots,
        "flags": flags,
        "customer_claims": [],
    }
    data.update(overrides)
    return data


@dataclass
class FakeOpenAI:
    """Replays queued responses (or exceptions) and keeps every request it received."""

    responses: list[httpx.Response | Exception] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if not self.responses:
            raise AssertionError("unexpected extra request to the model")
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    def bodies(self) -> list[dict[str, Any]]:
        return [json.loads(request.content) for request in self.requests]


@pytest.fixture
def fake() -> FakeOpenAI:
    return FakeOpenAI()


@pytest.fixture
def calls() -> list[ModelCall]:
    return []


def make_client(fake: FakeOpenAI, calls: list[ModelCall], max_retries: int = 2) -> OpenAIJsonClient:
    config = LLMClientConfig(
        api_key=API_KEY, model=MODEL, max_retries=max_retries, retry_wait_seconds=0
    )
    return OpenAIJsonClient(
        config, transport=httpx.MockTransport(fake.handler), recorder=calls.append
    )


@pytest.fixture
def adapter(fake: FakeOpenAI, calls: list[ModelCall]) -> OpenAILLMAdapter:
    return OpenAILLMAdapter(make_client(fake, calls))


def context(language: Language | None = Language.ES, pending: SlotName | None = None) -> LLMContext:
    return LLMContext(
        customer_ref="CUS-pseudonym-7f3a",
        language=language,
        masked_products=["****4821"],
        transactions=[
            LLMTransaction(
                transaction_ref="TRX-T1-PURCHASE",
                transaction_date=date(2026, 6, 16),
                amount=Decimal("50.00"),
                currency="USD",
                merchant_name="Cafe Sintetico",
                transaction_status="Approved",
            )
        ],
        pending_slot=pending,
    )
