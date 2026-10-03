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
    Slots,
)
from app.llm_adapter import prompts
from app.llm_adapter.adapter import (
    TRANSACTION_ID_DISCARDED,
    OpenAILLMAdapter,
    enforce_transaction_id,
    parse_extraction,
)
from app.orchestrator.corrections import apply_declined_correction
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
        # Validated after the deterministic backup of the Orchestrator (corrections.py): the
        # model may or may not extract the 40; the corrected amount is 40 either way.
        established = Slots(reason_code=ReasonCode.INCORRECT_AMOUNT, expected_amount=Decimal("30"))
        corrected = apply_declined_correction(established, result.slots, None, recorded["message"])
        ref = corrected.slots.transaction_ref
        assert Decimal("40") in {corrected.slots.expected_amount, ref.amount if ref else None}


# --- Side questions, "ese no es", block requests (extract@1.7.0), flow_help (extract@1.8.0) ---

SIDE_CASES: dict[str, dict[str, Any]] = {
    "es_side_refund": {"side_question": SideQuestion.REFUND},
    "es_side_refund_again": {"side_question": SideQuestion.REFUND},
    "es_answer_and_refund": {"side_question": SideQuestion.REFUND, "card_in_possession": True},
    "es_wrong_transaction": {"wrong_transaction": True},
    "es_block_requested": {"block_card_requested": True},
    "pt_side_other": {"side_question": SideQuestion.OTHER},
    "es_flow_help_name_only": {"side_question": SideQuestion.FLOW_HELP},
    "es_flow_help_what_data": {"side_question": SideQuestion.FLOW_HELP},
    "es_flow_help_no_amount": {"side_question": SideQuestion.FLOW_HELP},
    "es_side_other_account": {"side_question": SideQuestion.OTHER},
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


def test_the_approximate_amount_of_the_manual_test() -> None:
    name = "es_approximate_amount"
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"])
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    ref = result.slots.transaction_ref
    assert ref is not None and ref.amount == Decimal("40")
    assert ref.merchant is not None and "buen sabor" in ref.merchant.lower()
    assert ref.amount_approximate is True  # marked by the code, not by the model


# --- Periods of days and a generic merchant (extract@1.9.0) ----------------------------------

PERIOD_CASES = {
    "es_period_15_19": (date(2026, 6, 15), date(2026, 6, 17)),  # cut at the business date
    "es_period_mid_june": (date(2026, 6, 11), date(2026, 6, 17)),  # 11 to 20, cut
    "es_period_last_week": (date(2026, 6, 8), date(2026, 6, 14)),  # Monday to Sunday
    "pt_period_10_12": (date(2026, 6, 10), date(2026, 6, 12)),
}


@pytest.mark.parametrize(("name", "expected"), PERIOD_CASES.items())
def test_periods_of_days(name: str, expected: tuple[date, date]) -> None:
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"])
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    ref = result.slots.transaction_ref
    assert ref is not None and (ref.date_from, ref.date_to) == expected
    assert ref.transaction_date is None


def test_the_generic_restaurant_of_the_manual_test_3() -> None:
    name = "es_generic_restaurant"
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"])
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    ref = result.slots.transaction_ref
    assert ref is not None and ref.amount == Decimal("40") and ref.amount_approximate is True
    assert ref.merchant is not None and "restaurante" in ref.merchant.lower()
    # Whether the model also marks flow_help does not matter: the Orchestrator processes the
    # details and sends no help text (tests/orchestrator/test_manual_3.py).


# --- A stolen card is not account takeover (extract@1.11.0) -------------------------------------


def test_a_stolen_card_is_not_account_takeover() -> None:
    name = "pt_card_stolen"  # the words of scenario S013
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    pending = SlotName(recorded["pending_slot"])
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    assert result.slots.card_in_possession is False
    assert result.flags.account_takeover_reported is False


# --- Distinct unrecognized charges for the ESC-03 batch (extract@1.11.0) ------------------------

UNRECOGNIZED_CASES = {
    "es_three_unrecognized": 3,  # three charges in one message
    "es_same_charge_twice": 1,  # the same charge mentioned three times
    "pt_no_unrecognized": 0,  # a wrong amount, nothing unrecognized
}


@pytest.mark.parametrize(("name", "expected"), UNRECOGNIZED_CASES.items())
def test_distinct_unrecognized_charges(name: str, expected: int) -> None:
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    result, _ = extract(name, SMOKE_CONTEXT)
    assert result.unrecognized_reported == expected


# --- Claims: every fact about the dispute, no conversation talk (extract@1.12.0) ----------------


def claims_of(name: str) -> list[str]:
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION, name
    pending = SlotName(recorded["pending_slot"]) if recorded["pending_slot"] else None
    result, _ = extract(name, SMOKE_CONTEXT.model_copy(update={"pending_slot": pending}))
    return list(result.customer_claims)


FACT_CLAIMS = (
    "es_approximate_amount",  # "como de 40 dólares en El Buen Sabor"
    "es_answer_and_refund",  # "sí, la tengo"
    "es_generic_restaurant",  # "como de 40 dólares, en un restaurante"
    "pt_no_unrecognized",  # "eram 15 dólares"
    "pt_card_stolen",  # "acho que roubaram"
)


@pytest.mark.parametrize("name", FACT_CLAIMS)
def test_facts_about_the_dispute_are_claims(name: str) -> None:
    assert claims_of(name), f"{name}: the customer asserts a fact and no claim came out"


# The claims behind the evidence of ESC-03 (three charges), ESC-05 (the charge and the request
# for a human) and ESC-06 (the purchase, the contact and the lawyer).
EVIDENCE_CLAIMS = {"es_three_unrecognized": 3, "pt_duplicate_human": 2, "es_not_received_legal": 3}


@pytest.mark.parametrize(("name", "minimum"), EVIDENCE_CLAIMS.items())
def test_the_claims_behind_the_evidence(name: str, minimum: int) -> None:
    assert len(claims_of(name)) >= minimum


CONVERSATION_TALK = ("es_confirm_confirmed", "es_flow_help_what_data", "pt_side_other")


@pytest.mark.parametrize("name", CONVERSATION_TALK)
def test_conversation_talk_is_not_a_claim(name: str) -> None:
    assert claims_of(name) == []


# --- A stolen phone is account takeover (extract@1.13.0) ---------------------------------------


@pytest.mark.parametrize("name", ["es_stolen_phone", "pt_stolen_phone"])
def test_a_stolen_phone_is_account_takeover(name: str) -> None:
    assert (FIXTURES / f"{name}.json").exists(), (
        f"{name} is not recorded yet: run `python scripts/llm_smoke.py --record`"
    )
    recorded = fixture(name)
    assert recorded["prompt_version"] == prompts.EXTRACT_PROMPT_VERSION
    result, _ = extract(name, SMOKE_CONTEXT)
    assert result.flags.account_takeover_reported  # the model, and the rules besides it
