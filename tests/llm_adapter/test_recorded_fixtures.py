"""Parser regression tests on answers recorded from the real model.

tests/fixtures/llm/ holds raw answers of ``gpt-6-luna`` to the three smoke messages
(``scripts/llm_smoke.py --record``, synthetic data only), recorded with the current extraction
prompt. They pin how the parser and the deterministic rules treat real model output. History
of these fixtures (evidence for M17) is in reports/m5_llm_extraction_evidence.json.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.contracts import (
    Confirmation,
    Language,
    LLMContext,
    LLMTransaction,
    ModelCall,
    ReasonCode,
    SideQuestion,
    SlotName,
)
from app.llm_adapter import prompts
from app.llm_adapter.adapter import (
    TRANSACTION_ID_DISCARDED,
    OpenAILLMAdapter,
    enforce_transaction_id,
    parse_extraction,
)
from tests.llm_adapter.conftest import FakeOpenAI, completion, make_client

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "llm"
CASES = ("es_unrecognized", "pt_duplicate_human", "es_not_received_legal")
BUSINESS_DATE = date(2026, 6, 17)

# The context the smoke script sends (scripts/llm_smoke.py).
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
    business_date=BUSINESS_DATE,
)  # fmt: skip


def fixture(name: str) -> dict[str, Any]:
    data: dict[str, Any] = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return data


def ref_id(result: Any) -> str | None:
    ref = result.slots.transaction_ref
    assert ref is not None
    return ref.transaction_id  # type: ignore[no-any-return]


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


def with_proposed_id(name: str, transaction_id: str) -> dict[str, Any]:
    """The recorded answer with a transaction_id the model might have proposed."""
    raw = fixture(name)["raw"]
    ref = {**raw["slots"]["transaction_ref"], "transaction_id": transaction_id}
    return {**raw, "slots": {**raw["slots"], "transaction_ref": ref}}


def test_fixtures_are_recorded_with_the_current_prompt_and_the_real_model() -> None:
    for name in CASES:
        recorded = fixture(name)
        assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION, name
        assert recorded["response_model"] == "gpt-6-luna"
        assert isinstance(recorded["raw"], dict)


@pytest.mark.parametrize("name", CASES)
def test_raw_answers_pass_schema_validation(name: str) -> None:
    parse_extraction(fixture(name)["raw"], BUSINESS_DATE)


def test_es_unrecognized() -> None:
    result, calls = extract("es_unrecognized")
    ref = result.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_id is None  # the model no longer proposes one
    assert ref.transaction_date == date(2026, 6, 16)  # "del 16 de junio", year from code
    assert ref.amount == Decimal("50")
    assert ref.merchant == "Cafe Sintetico"
    assert result.slots.reason_code is ReasonCode.UNRECOGNIZED
    assert result.slots.card_in_possession is True
    assert result.slots.shared_credentials is False
    assert result.detected_language == "es"
    assert all(claim.startswith("El cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == []


def test_pt_duplicate_human() -> None:
    result, calls = extract("pt_duplicate_human")
    ref = result.slots.transaction_ref
    assert ref is not None
    assert ref.transaction_id is None
    assert ref.transaction_date is None  # the customer gave no date
    assert ref.amount == Decimal("18.9")
    assert ref.merchant == "Streaming Plus"
    assert result.slots.reason_code is ReasonCode.DUPLICATE
    assert result.flags.human_requested is True
    assert result.detected_language == "pt"
    assert all(claim.startswith("O cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == []


def test_es_not_received_legal() -> None:
    result, calls = extract("es_not_received_legal")
    assert result.slots.transaction_ref is None  # the customer named no transaction
    assert result.slots.reason_code is ReasonCode.NOT_RECEIVED
    assert result.slots.expected_delivery_date == date(2026, 6, 1)
    assert result.slots.merchant_contacted is True
    assert result.flags.legal_or_vulnerability is True
    assert all(claim.startswith("El cliente") for claim in result.customer_claims)
    assert calls[0].adjustments == []


def test_guessed_real_id_is_dropped_even_if_that_transaction_was_shown() -> None:
    # The model sees aliases only; a real ID the customer did not give is a guess.
    parsed, _ = parse_extraction(with_proposed_id("es_unrecognized", "TRX-SMOKE-1"), BUSINESS_DATE)
    message = fixture("es_unrecognized")["message"]
    enforced, adjustments = enforce_transaction_id(parsed, message, {"C1": "TRX-SMOKE-1"})
    assert ref_id(enforced) is None
    assert adjustments == (TRANSACTION_ID_DISCARDED,)


def test_alias_of_a_shown_candidate_is_translated() -> None:
    parsed, _ = parse_extraction(with_proposed_id("es_unrecognized", "C1"), BUSINESS_DATE)
    enforced, adjustments = enforce_transaction_id(
        parsed, "la primera", {"C1": "TRX-SMOKE-1", "C2": "TRX-SMOKE-2"}
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
    parsed, _ = parse_extraction(with_proposed_id("es_unrecognized", "TRX-SMOKE-1"), BUSINESS_DATE)
    enforced, adjustments = enforce_transaction_id(parsed, message, {})
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
    parsed, _ = parse_extraction(only_id, BUSINESS_DATE)
    enforced, _ = enforce_transaction_id(parsed, "esa", {})
    assert enforced.slots.transaction_ref is None


# --- Replies to a pending question (extract@1.6.0) -------------------------------------------

CONFIRMATION_CASES = {
    "es_confirm_confirmed": Confirmation.CONFIRMED,
    "es_confirm_declined_amount": Confirmation.DECLINED,
    "es_confirm_withdrawn": Confirmation.WITHDRAWN,
    "pt_confirm_withdrawn": Confirmation.WITHDRAWN,
    "es_confirm_hedged": Confirmation.HEDGED,
    "es_duplicate_ref_confirmed": Confirmation.CONFIRMED,  # pending slot: duplicate_ref
}


@pytest.mark.parametrize(("name", "expected"), CONFIRMATION_CASES.items())
def test_confirmation_replies(name: str, expected: Confirmation) -> None:
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"])
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    assert result.slots.confirmation is expected
    if name == "es_confirm_declined_amount":
        ref = result.slots.transaction_ref
        corrected = {result.slots.expected_amount, ref.amount if ref else None}
        assert Decimal("40") in corrected


# --- Side questions, "ese no es" and block requests (extract@1.7.0) ---------------------------

SIDE_CASES: dict[str, dict[str, Any]] = {
    "es_side_refund": {"side_question": SideQuestion.REFUND},
    "es_side_refund_again": {"side_question": SideQuestion.REFUND},
    "es_answer_and_refund": {"side_question": SideQuestion.REFUND, "card_in_possession": True},
    "es_wrong_transaction": {"wrong_transaction": True},
    "es_block_requested": {"block_card_requested": True},
    "pt_side_other": {"side_question": SideQuestion.OTHER},
}


@pytest.mark.parametrize(("name", "expected"), SIDE_CASES.items())
def test_side_questions_and_block_replies(name: str, expected: dict[str, Any]) -> None:
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"]) if recorded["pending_slot"] else None
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    assert result.side_question is expected.get("side_question")
    assert result.wrong_transaction is expected.get("wrong_transaction", False)
    assert result.block_card_requested is expected.get("block_card_requested", False)
    assert result.slots.card_in_possession is expected.get("card_in_possession")
