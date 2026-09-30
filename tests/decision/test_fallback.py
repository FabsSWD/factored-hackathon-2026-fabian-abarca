from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
from pydantic import SecretStr

from app.contracts import (
    ConversationFlags,
    ExtractionResult,
    ModelCall,
    ModelSignals,
    ModelSource,
    ReasonCode,
    Slots,
    TransactionRef,
)
from app.decision import derive_fallback, resolve_signals
from app.decision.factory import decision_client_from_settings
from app.settings import Settings
from tests.decision.conftest import FakeKev, context, fixture, make_client


def extraction(**overrides: object) -> ExtractionResult:
    return ExtractionResult(**overrides)  # type: ignore[arg-type]


def test_fallback_from_an_extracted_reason_code() -> None:
    result = derive_fallback(extraction(slots=Slots(reason_code=ReasonCode.DUPLICATE)))
    assert result.source is ModelSource.LLM_FALLBACK
    assert result.model_version == "llm_fallback@1.0.0"
    assert result.reason_code_probs == {ReasonCode.DUPLICATE: 1.0}
    assert result.reason_code_other is None
    assert result.ambiguity == 0.0
    assert result.escalation_risk == 0.0


def test_unstated_reason_is_unknown_not_other() -> None:
    result = derive_fallback(extraction())
    assert result.reason_code_probs == {}
    assert result.reason_code_other is None  # not "outside scope"
    assert result.ambiguity == 1.0


def test_a_transaction_reference_alone_is_not_ambiguous() -> None:
    slots = Slots(transaction_ref=TransactionRef(transaction_date=date(2026, 6, 16)))
    result = derive_fallback(extraction(slots=slots))
    assert result.ambiguity == 0.0
    assert result.reason_code_probs == {}


@pytest.mark.parametrize(
    "flags",
    [
        ConversationFlags(human_requested=True),
        ConversationFlags(legal_or_vulnerability=True),
        ConversationFlags(account_takeover_reported=True),
    ],
)
def test_urgent_flags_raise_escalation_risk(flags: ConversationFlags) -> None:
    assert derive_fallback(extraction(flags=flags)).escalation_risk == 1.0


def test_authentication_declined_is_not_urgent() -> None:
    flags = ConversationFlags(authentication_declined=True)
    assert derive_fallback(extraction(flags=flags)).escalation_risk == 0.0


def test_fallback_values_are_zero_or_one() -> None:
    result = derive_fallback(
        extraction(
            slots=Slots(reason_code=ReasonCode.FEE), flags=ConversationFlags(human_requested=True)
        )
    )
    values = [*result.reason_code_probs.values(), result.ambiguity, result.escalation_risk]
    assert set(values) <= {0.0, 1.0}


# --- Resolution for a turn -------------------------------------------------------------------


KEV = ModelSignals(
    source=ModelSource.KEV, reason_code_probs={ReasonCode.FEE: 0.9}, reason_code_other=0.1
)
UNAVAILABLE = ModelSignals(source=ModelSource.UNAVAILABLE)


def test_kev_wins_when_it_answered() -> None:
    assert resolve_signals(KEV, extraction(slots=Slots(reason_code=ReasonCode.DUPLICATE))) == KEV


def test_fallback_when_kev_is_unavailable() -> None:
    result = resolve_signals(UNAVAILABLE, extraction(slots=Slots(reason_code=ReasonCode.FEE)))
    assert result.source is ModelSource.LLM_FALLBACK


def test_both_failed_is_unavailable_without_exception() -> None:
    assert resolve_signals(UNAVAILABLE, None) == UNAVAILABLE


def test_rule_only_extraction_after_an_llm_failure_still_feeds_the_fallback() -> None:
    # ExtractionUnavailableError.fallback: empty slots and the rule signals.
    rule_only = ExtractionResult(flags=ConversationFlags(human_requested=True))
    result = resolve_signals(UNAVAILABLE, rule_only)
    assert result.source is ModelSource.LLM_FALLBACK
    assert result.escalation_risk == 1.0
    assert result.reason_code_probs == {}


def test_kev_down_end_to_end_uses_the_fallback(fake: FakeKev, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(500)]
    kev = asyncio.run(make_client(fake, calls).signals("Me cobraron dos veces", context()))
    extracted = extraction(slots=Slots(reason_code=ReasonCode.DUPLICATE))
    result = resolve_signals(kev, extracted)
    assert result.source is ModelSource.LLM_FALLBACK
    assert result.reason_code_probs == {ReasonCode.DUPLICATE: 1.0}
    assert calls[0].error == "HTTP 500"


def test_kev_up_end_to_end(fake: FakeKev, calls: list[ModelCall]) -> None:
    fake.responses = [httpx.Response(200, json=fixture("kev_pt_response.json"))]
    kev = asyncio.run(make_client(fake, calls).signals("Fui cobrado duas vezes", context()))
    assert resolve_signals(kev, extraction()).source is ModelSource.KEV


# --- Factory --------------------------------------------------------------------------------


def test_factory_with_and_without_kev() -> None:
    base = {"_env_file": None, "business_date": date(2026, 6, 17), "openai_api_key": SecretStr("x")}
    configured = decision_client_from_settings(
        Settings(**base, kev_base_url="http://kev:8008", kev_timeout_seconds=1.5)  # type: ignore[arg-type]
    )
    assert configured.configured
    unconfigured = decision_client_from_settings(Settings(**base))  # type: ignore[arg-type]
    assert not unconfigured.configured
