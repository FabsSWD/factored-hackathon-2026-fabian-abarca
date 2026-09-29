"""Parser regression tests on answers recorded from the real model.

tests/fixtures/llm/ holds raw answers of ``gpt-6-luna`` to the three smoke messages
(``scripts/llm_smoke.py --record``, prompt extract@1.1.0, synthetic data only). They pin how
the parser and the deterministic rules treat real model output: in particular the model
filled ``transaction_id`` although the customer neither gave it nor picked a shown candidate,
and the code must discard it while keeping date, amount and merchant.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.contracts import Language, LLMContext, LLMTransaction, ModelCall, ReasonCode
from app.llm_adapter.adapter import (
    TRANSACTION_ID_DISCARDED,
    OpenAILLMAdapter,
    enforce_transaction_id,
    parse_extraction,
)
from tests.llm_adapter.conftest import FakeOpenAI, completion, make_client

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "llm"
CASES = ("es_unrecognized", "pt_duplicate_human", "es_not_received_legal")

# The context the smoke script sent (scripts/llm_smoke.py).
SMOKE_CONTEXT = LLMContext(
    customer_ref="CUS-smoke-0001",
    masked_products=["****4821"],
    transactions=[
        LLMTransaction(transaction_ref="TRX-SMOKE-1", transaction_date=date(2026, 6, 16),
                       amount=Decimal("50.00"), currency="USD", merchant_name="Cafe Sintetico",
                       transaction_status="Approved"),
        LLMTransaction(transaction_ref="TRX-SMOKE-2", transaction_date=date(2026, 6, 15),
                       amount=Decimal("18.90"), currency="USD", merchant_name="Streaming Plus",
                       transaction_status="Approved"),
    ],
)  # fmt: skip


def ref_id(result: Any) -> str | None:
    ref = result.slots.transaction_ref
    assert ref is not None
    return ref.transaction_id  # type: ignore[no-any-return]


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def extract(name: str, context: LLMContext = SMOKE_CONTEXT) -> tuple[Any, list[ModelCall]]:
    recorded = fixture(name)
    fake = FakeOpenAI(responses=[completion(recorded["raw"])])
    calls: list[ModelCall] = []
    adapter = OpenAILLMAdapter(make_client(fake, calls, max_retries=0))
    language = Language(recorded["language"])
    result = asyncio.run(
        adapter.extract(recorded["message"], context.model_copy(update={"language": language}))
    )
    return result, calls


def test_all_three_fixtures_are_present_and_recorded_with_the_real_model() -> None:
    for name in CASES:
        recorded = fixture(name)
        assert recorded["prompt_version"] == "extract@1.1.0"
        assert recorded["response_model"] == "gpt-6-luna"
        assert isinstance(recorded["raw"], dict)


@pytest.mark.parametrize("name", CASES)
def test_raw_answers_pass_schema_validation(name: str) -> None:
    parse_extraction(fixture(name)["raw"])


def test_es_unrecognized_drops_the_guessed_transaction_id() -> None:
    assert fixture("es_unrecognized")["raw"]["slots"]["transaction_ref"]["transaction_id"] == (
        "TRX-SMOKE-1"
    )
    result, calls = extract("es_unrecognized")
    ref = result.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_id is None
    assert ref.transaction_date == date(2026, 6, 16)
    assert ref.amount == Decimal("50")
    assert ref.merchant == "Cafe Sintetico"
    assert result.slots.reason_code is ReasonCode.UNRECOGNIZED
    assert result.slots.card_in_possession is True
    assert result.slots.shared_credentials is False
    assert result.detected_language == "es"
    assert all(claim.startswith("El cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == [TRANSACTION_ID_DISCARDED]


def test_pt_duplicate_human_drops_the_guessed_transaction_id() -> None:
    result, calls = extract("pt_duplicate_human")
    ref = result.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_id is None
    assert ref.transaction_date is None
    assert ref.amount == Decimal("18.9")
    assert ref.merchant == "Streaming Plus"
    assert result.slots.reason_code is ReasonCode.DUPLICATE
    assert result.flags.human_requested is True
    assert result.detected_language == "pt"
    assert all(claim.startswith("O cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == [TRANSACTION_ID_DISCARDED]


def test_es_not_received_legal_keeps_its_slots_and_signal() -> None:
    result, calls = extract("es_not_received_legal")
    assert result.slots.transaction_ref is None  # the customer named no transaction
    assert result.slots.reason_code is ReasonCode.NOT_RECEIVED
    assert result.slots.expected_delivery_date == date(2026, 6, 1)
    assert result.slots.merchant_contacted is True
    assert result.flags.legal_or_vulnerability is True
    assert all(claim.startswith("El cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == []


def test_recorded_real_id_is_dropped_even_if_that_transaction_was_shown() -> None:
    # The model saw aliases only; a real ID it did not get from the customer is a guess.
    shown = SMOKE_CONTEXT.model_copy(update={"shown_candidates": ["TRX-SMOKE-1", "TRX-SMOKE-2"]})
    result, calls = extract("es_unrecognized", shown)
    assert ref_id(result) is None
    assert calls[0].adjustments == [TRANSACTION_ID_DISCARDED]


def test_alias_of_a_shown_candidate_is_translated() -> None:
    raw = fixture("es_unrecognized")["raw"]
    aliased = {**raw, "slots": {**raw["slots"], "transaction_ref": {
        **raw["slots"]["transaction_ref"], "transaction_id": "C1"}}}  # fmt: skip
    enforced, adjustments = enforce_transaction_id(
        parse_extraction(aliased), "la primera", {"C1": "TRX-SMOKE-1", "C2": "TRX-SMOKE-2"}
    )
    assert ref_id(enforced) == "TRX-SMOKE-1"
    assert adjustments == ()


@pytest.mark.parametrize(
    ("message", "kept"),
    [
        ("No reconozco TRX-SMOKE-1", True),
        ("no reconozco trx-smoke-1, gracias", True),
        ("No reconozco TRX-SMOKE-10", False),
        ("No reconozco XTRX-SMOKE-1", False),
        ("No reconozco el cargo", False),
    ],
)
def test_transaction_id_given_literally_in_the_message(message: str, kept: bool) -> None:
    result = parse_extraction(fixture("es_unrecognized")["raw"])
    enforced, adjustments = enforce_transaction_id(result, message, {})
    assert (ref_id(enforced) is not None) is kept
    assert adjustments == (() if kept else (TRANSACTION_ID_DISCARDED,))


def test_reference_with_only_a_transaction_id_becomes_none_when_dropped() -> None:
    raw = fixture("es_unrecognized")["raw"]
    only_id = {
        **raw,
        "slots": {
            **raw["slots"],
            "transaction_ref": {"transaction_id": "TRX-SMOKE-1", "transaction_date": None,
                                "amount": None, "merchant": None},
        },
    }  # fmt: skip
    enforced, _ = enforce_transaction_id(parse_extraction(only_id), "esa", {})
    assert enforced.slots.transaction_ref is None
