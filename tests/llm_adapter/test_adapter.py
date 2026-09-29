from __future__ import annotations

import asyncio
import json
from collections.abc import Coroutine
from datetime import date
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app import interfaces
from app.contracts import (
    Confirmation,
    ExtractionResult,
    Language,
    LLMContext,
    LLMTransaction,
    ModelCall,
    ReasonCode,
    SlotName,
    TransactionRef,
)
from app.llm_adapter import prompts
from app.llm_adapter.adapter import CONNECT_PURPOSE, EXTRACT_PURPOSE, OpenAILLMAdapter
from app.llm_adapter.client import LLMError
from tests.llm_adapter.conftest import (
    MODEL,
    FakeOpenAI,
    completion,
    context,
    extraction,
    make_client,
)


def run[T](coroutine: Coroutine[Any, Any, T]) -> T:
    return asyncio.run(coroutine)


def extract(
    adapter: OpenAILLMAdapter, message: str, ctx: LLMContext | None = None
) -> ExtractionResult:
    return run(adapter.extract(message, ctx or context()))


def sent_user(fake: FakeOpenAI, index: int = 0) -> dict[str, Any]:
    body = fake.bodies()[index]
    return json.loads(body["messages"][1]["content"])  # type: ignore[no-any-return]


# --- Extraction: 14 simulated messages in es and pt ------------------------------------

CASES: list[tuple[str, str, dict[str, Any], dict[str, Any]]] = [
    (
        "es-unrecognized",
        "No reconozco el cargo de 50 dólares en Cafe Sintetico, tengo mi tarjeta y no di "
        "mis claves",
        extraction(
            slots={
                "transaction_ref": {
                    "transaction_id": "TRX-T1-PURCHASE",
                    "transaction_date": None,
                    "amount": None,
                    "merchant": None,
                },
                "reason_code": "RC_UNRECOGNIZED",
                "card_in_possession": True,
                "shared_credentials": False,
            },
            customer_claims=["No hizo la compra"],
        ),
        {
            "reason_code": ReasonCode.UNRECOGNIZED,
            "card_in_possession": True,
            "shared_credentials": False,
            "transaction_ref": TransactionRef(transaction_id="TRX-T1-PURCHASE"),
        },
    ),
    (
        "es-duplicate",
        "Me cobraron dos veces 18,90 dólares en Streaming Plus el 15 de junio de 2026",
        extraction(
            slots={
                "transaction_ref": {
                    "transaction_id": None,
                    "transaction_date": "2026-06-15",
                    "amount": 18.9,
                    "merchant": "Streaming Plus",
                },
                "reason_code": "RC_DUPLICATE",
            }
        ),
        {
            "reason_code": ReasonCode.DUPLICATE,
            "transaction_ref": TransactionRef(
                transaction_date=date(2026, 6, 15),
                amount=Decimal("18.9"),
                merchant="Streaming Plus",
            ),
        },
    ),
    (
        "es-incorrect-amount",
        "Acordé pagar 180,50 pero me cobraron 200",
        extraction(slots={"reason_code": "RC_INCORRECT_AMOUNT", "expected_amount": 180.5}),
        {"reason_code": ReasonCode.INCORRECT_AMOUNT, "expected_amount": Decimal("180.5")},
    ),
    (
        "es-not-received",
        "Nunca llegó mi pedido, debía llegar el 2026-06-01 y ya hablé con la tienda",
        extraction(
            slots={
                "reason_code": "RC_NOT_RECEIVED",
                "expected_delivery_date": "2026-06-01",
                "merchant_contacted": True,
            }
        ),
        {
            "reason_code": ReasonCode.NOT_RECEIVED,
            "expected_delivery_date": date(2026, 6, 1),
            "merchant_contacted": True,
        },
    ),
    (
        "es-fee",
        "Me cobraron una comisión por manejo de cuenta que no corresponde",
        extraction(slots={"reason_code": "RC_FEE", "fee_ref": "comisión por manejo de cuenta"}),
        {"reason_code": ReasonCode.FEE, "fee_ref": "comisión por manejo de cuenta"},
    ),
    (
        "es-confirmed",
        "Sí, confirmo",
        extraction(slots={"confirmation": "confirmed"}),
        {"confirmation": Confirmation.CONFIRMED},
    ),
    (
        "es-hedged",
        "Creo que sí",
        extraction(slots={"confirmation": "hedged"}),
        {"confirmation": Confirmation.HEDGED},
    ),
    (
        "es-declined",
        "No, eso no es correcto",
        extraction(slots={"confirmation": "declined"}),
        {"confirmation": Confirmation.DECLINED},
    ),
    (
        "pt-unrecognized-takeover",
        "Não reconheço essa compra, roubaram meu celular ontem",
        extraction(
            detected_language="pt",
            slots={"reason_code": "RC_UNRECOGNIZED"},
            flags={"account_takeover_reported": True},
        ),
        {"reason_code": ReasonCode.UNRECOGNIZED},
    ),
    (
        "pt-duplicate",
        "Fui cobrado duas vezes pela mesma assinatura",
        extraction(detected_language="pt", slots={"reason_code": "RC_DUPLICATE"}),
        {"reason_code": ReasonCode.DUPLICATE},
    ),
    (
        "pt-confirmed",
        "Sim, confirmo",
        extraction(detected_language="pt", slots={"confirmation": "confirmed"}),
        {"confirmation": Confirmation.CONFIRMED},
    ),
    (
        "pt-not-received",
        "Paguei e o produto não chegou; a entrega era em 2026-06-05 e não falei com a loja",
        extraction(
            detected_language="pt",
            slots={
                "reason_code": "RC_NOT_RECEIVED",
                "expected_delivery_date": "2026-06-05",
                "merchant_contacted": False,
            },
        ),
        {
            "reason_code": ReasonCode.NOT_RECEIVED,
            "expected_delivery_date": date(2026, 6, 5),
            "merchant_contacted": False,
        },
    ),
    (
        "es-human-legal",
        "Quiero hablar con un humano, si no voy a llamar a mi abogado",
        extraction(flags={"human_requested": True, "legal_or_vulnerability": True}),
        {},
    ),
    (
        "pt-auth-declined",
        "Não vou informar meu documento",
        extraction(detected_language="pt", flags={"authentication_declined": True}),
        {},
    ),
]


@pytest.mark.parametrize(
    ("message", "answer", "expected_slots"),
    [(message, answer, expected) for _, message, answer, expected in CASES],
    ids=[case_id for case_id, *_ in CASES],
)
def test_extraction_in_spanish_and_portuguese(
    adapter: OpenAILLMAdapter,
    fake: FakeOpenAI,
    message: str,
    answer: dict[str, Any],
    expected_slots: dict[str, Any],
) -> None:
    fake.responses = [completion(answer)]
    result = extract(adapter, message)
    assert result.detected_language == answer["detected_language"]
    for slot, value in expected_slots.items():
        assert getattr(result.slots, slot) == value, slot
    filled = {name for name, value in result.slots.model_dump().items() if value is not None}
    assert filled == set(expected_slots)
    for flag, value in answer["flags"].items():
        assert getattr(result.flags, flag) is value
    assert result.customer_claims == answer["customer_claims"]


def test_mixed_language_is_reported_as_ambiguous(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI
) -> None:
    fake.responses = [completion(extraction(detected_language=None, language_ambiguous=True))]
    result = extract(adapter, "Hola, obrigado, cobro errado")
    assert result.detected_language is None
    assert result.language_ambiguous is True


def test_empty_transaction_reference_becomes_none(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI
) -> None:
    empty = {"transaction_id": None, "transaction_date": None, "amount": None, "merchant": ""}
    fake.responses = [completion(extraction(slots={"transaction_ref": empty}))]
    assert extract(adapter, "Hola").slots.transaction_ref is None


def test_claims_are_bounded(adapter: OpenAILLMAdapter, fake: FakeOpenAI) -> None:
    claims = ["  ", "a" * 500, *[f"claim {i}" for i in range(10)]]
    fake.responses = [completion(extraction(customer_claims=claims))]
    result = extract(adapter, "Hola")
    assert len(result.customer_claims) == 5
    assert result.customer_claims[0] == "a" * 200
    assert all(claim.strip() for claim in result.customer_claims)


# --- Values outside the schema ---------------------------------------------------------

OUT_OF_SCHEMA = [
    extraction(slots={"reason_code": "RC_OTHER"}),
    extraction(slots={"expected_amount": -5}),
    extraction(slots={"expected_amount": "cien"}),
    extraction(slots={"expected_delivery_date": "16/06/2026"}),
    extraction(slots={"confirmation": "yes"}),
    extraction(slots={"card_in_possession": "maybe"}),
    extraction(
        slots={
            "transaction_ref": {
                "transaction_id": None,
                "transaction_date": None,
                "amount": 0,
                "merchant": None,
            }
        }
    ),
    extraction(detected_language="ES"),
    extraction(detected_language="spanish"),
    extraction(flags={"human_requested": "yes"}),
    extraction(slots={"invented_slot": "x"}),
]


@pytest.mark.parametrize("answer", OUT_OF_SCHEMA)
def test_values_outside_the_schema_are_rejected(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, calls: list[ModelCall], answer: dict[str, Any]
) -> None:
    fake.responses = [completion(answer)] * 3
    with pytest.raises(LLMError):
        extract(adapter, "Hola")
    assert len(fake.requests) == 3
    assert all(call.error == "invalid output" for call in calls)


def test_one_bad_answer_then_a_good_one(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [
        completion(extraction(slots={"reason_code": "RC_OTHER"})),
        completion(extraction(slots={"reason_code": "RC_FEE"})),
    ]
    assert extract(adapter, "Hola").slots.reason_code is ReasonCode.FEE
    assert [call.success for call in calls] == [False, True]


def test_timeout_429_and_500_end_in_a_controlled_error(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [httpx.ReadTimeout("slow"), httpx.Response(429), httpx.Response(500)]
    with pytest.raises(LLMError):
        extract(adapter, "Hola")
    assert [call.error for call in calls] == ["timeout", "HTTP 429", "HTTP 500"]


# --- Outgoing request: DATA-01 ---------------------------------------------------------

PROHIBITED_KEYS = (
    "document_number",
    "date_of_birth",
    "address",
    "mobile_phone",
    "email",
    "first_name",
    "last_name",
    "full_name",
    "customer_id",
    "segment",
    "credit_score",
    "gender",
)


def test_request_has_model_prompt_and_strict_schema(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [completion(extraction())]
    extract(adapter, "Hola", context(pending=SlotName.CONFIRMATION))
    (body,) = fake.bodies()
    assert body["model"] == MODEL
    assert body["messages"][0]["content"] == prompts.EXTRACT_SYSTEM
    assert body["response_format"]["json_schema"]["schema"] == prompts.EXTRACT_SCHEMA
    user = sent_user(fake)
    assert user["context"] == {
        "customer_ref": "CUS-pseudonym-7f3a",
        "language": "es",
        "masked_products": ["****4821"],
        "transactions": [
            {
                "transaction_ref": "TRX-T1-PURCHASE",
                "transaction_date": "2026-06-16",
                "amount": "50.00",
                "currency": "USD",
                "merchant_name": "Cafe Sintetico",
                "transaction_status": "Approved",
            }
        ],
        "pending_slot": "confirmation",
    }
    assert calls[0].purpose == EXTRACT_PURPOSE
    assert calls[0].prompt_version == prompts.EXTRACT_PROMPT_VERSION


def test_fields_attached_by_mistake_never_leave(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI
) -> None:
    leaked = LLMContext.model_construct(  # type: ignore[call-arg]
        customer_ref="CUS-pseudonym-7f3a",
        language=Language.ES,
        masked_products=["****4821"],
        transactions=[
            LLMTransaction.model_construct(  # type: ignore[call-arg]
                transaction_ref="TRX-1",
                transaction_date=date(2026, 6, 16),
                amount=Decimal("10"),
                currency="USD",
                merchant_name="Tienda",
                transaction_status="Approved",
                customer_id="CLI-SECRET00001",
                latitude="4.6803337",
            )
        ],
        pending_slot=None,
        email="ana@example.test",
        document_number="X1234567",
        date_of_birth="1990-06-18",
        first_name="Ana",
        address="Calle Falsa 123",
    )
    fake.responses = [completion(extraction())]
    extract(adapter, "Hola", leaked)
    raw = fake.requests[0].content.decode()
    for secret in (
        "ana@example.test",
        "X1234567",
        "1990-06-18",
        "Calle Falsa",
        "CLI-SECRET00001",
        "4.6803337",
        '"Ana"',
    ):
        assert secret not in raw
    sent = json.dumps(sent_user(fake))
    for key in PROHIBITED_KEYS:
        assert f'"{key}"' not in sent


def test_personal_data_in_the_message_is_scrubbed(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI
) -> None:
    message = (
        "Mi correo es ana@example.test, mi cédula es 1023456789, llámenme al +57 315 564 6977. "
        "Nací el 18/06/1990. No reconozco el cargo de 250.000 COP en la tarjeta "
        "4111 1111 1111 4821 del 16/06/2026."
    )
    fake.responses = [completion(extraction())]
    extract(adapter, message)
    raw = fake.requests[0].content.decode()
    for secret in (
        "ana@example.test",
        "1023456789",
        "315 564 6977",
        "18/06/1990",
        "4111 1111 1111 4821",
        "4111111111114821",
    ):
        assert secret not in raw
    sent = sent_user(fake)["message"]
    assert "****4821" in sent
    assert "250.000 COP" in sent
    assert "16/06/2026" in sent


# --- Connecting sentences ----------------------------------------------------------------

TEMPLATE = "Voy a transferir su caso a un agente, que ya tendrá la información que me compartió."


def connect(adapter: OpenAILLMAdapter, ctx: LLMContext | None = None) -> str:
    return run(adapter.connect(TEMPLATE, "Quiero hablar con alguien", ctx or context()))


def test_connect_wraps_the_template_unchanged(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, calls: list[ModelCall]
) -> None:
    fake.responses = [
        completion({"before": "Entiendo su situación.", "after": "Gracias por su paciencia."})
    ]
    assert connect(adapter) == f"Entiendo su situación. {TEMPLATE} Gracias por su paciencia."
    user = sent_user(fake)
    assert user == {
        "language": "es",
        "customer_message": "Quiero hablar con alguien",
        "fixed_message": TEMPLATE,
    }
    assert calls[0].purpose == CONNECT_PURPOSE
    assert calls[0].prompt_version == prompts.CONNECT_PROMPT_VERSION


def test_connect_ignores_any_attempt_to_rewrite_the_template(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI
) -> None:
    fake.responses = [completion({"before": "", "after": ""})]
    assert connect(adapter) == TEMPLATE


@pytest.mark.parametrize(
    "sentence",
    [
        "Le reembolsaremos el dinero.",  # COM-05
        "Su caso se resolverá en 3 días.",  # digits
        "Según la regla ESC-05 lo transfiero.",  # rule identifier
        "Entiendo " + "mucho " * 40,  # too long
        "Hola {nombre}.",  # template braces
    ],
)
def test_doubtful_sentences_are_dropped_not_repaired(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, sentence: str
) -> None:
    fake.responses = [completion({"before": sentence, "after": "Gracias."})]
    assert connect(adapter) == f"{TEMPLATE} Gracias."


@pytest.mark.parametrize(
    "failure",
    [
        [httpx.Response(500)] * 3,
        [completion("not json")] * 3,
        [completion({"before": 1, "after": None})] * 3,
        [httpx.Response(401)],
    ],
    ids=["500", "invalid-json", "wrong-types", "401"],
)
def test_connect_falls_back_to_the_template_alone(
    adapter: OpenAILLMAdapter, fake: FakeOpenAI, failure: list[httpx.Response]
) -> None:
    fake.responses = list(failure)
    assert connect(adapter) == TEMPLATE


def test_connect_uses_the_context_language(adapter: OpenAILLMAdapter, fake: FakeOpenAI) -> None:
    fake.responses = [completion({"before": "", "after": ""})] * 2
    connect(adapter, context(Language.PT))
    connect(adapter, context(None))
    assert [sent_user(fake, i)["language"] for i in range(2)] == ["pt", "es"]


def test_connect_scrubs_the_customer_message(adapter: OpenAILLMAdapter, fake: FakeOpenAI) -> None:
    fake.responses = [completion({"before": "", "after": ""})]
    run(adapter.connect(TEMPLATE, "Escríbanme a ana@example.test", context()))
    assert "ana@example.test" not in fake.requests[0].content.decode()


def test_adapter_implements_the_module_interface(adapter: OpenAILLMAdapter) -> None:
    assert isinstance(adapter, interfaces.LLMAdapter)


def test_extraction_with_the_default_client_factory(fake: FakeOpenAI) -> None:
    calls: list[ModelCall] = []
    fake.responses = [completion(extraction(slots={"reason_code": "RC_FEE"}))]
    adapter = OpenAILLMAdapter(make_client(fake, calls, max_retries=0))
    assert extract(adapter, "comisión").slots.reason_code is ReasonCode.FEE
    assert calls[0].input_tokens == 120
    assert calls[0].output_tokens == 40
    assert calls[0].latency_ms >= 0
