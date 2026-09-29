from __future__ import annotations

import pytest

from app.contracts import LLMContext
from app.llm_adapter.minimization import (
    ALLOWED_CONTEXT_FIELDS,
    context_payload,
    scrub_message,
)
from tests.llm_adapter.conftest import context


@pytest.mark.parametrize(
    ("message", "removed", "kept"),
    [
        ("Mi correo es ana.perez+banco@example.com", "ana.perez+banco@example.com", "[email]"),
        ("Tarjeta 4111 1111 1111 4821 con un cobro", "4111 1111 1111 4821", "****4821"),
        ("Tarjeta 4111-1111-1111-4821", "4111-1111-1111-4821", "****4821"),
        ("Mi cédula es 1023456789", "1023456789", "[documento]"),
        ("Mi documento: X1234567", "X1234567", "[documento]"),
        ("Mi DNI 42.388.496", "42.388.496", "[documento]"),
        ("Meu CPF é 123.456.789-00", "123.456.789-00", "[documento]"),
        ("CPF 123.456.789-00 sem palavra", "123.456.789-00", "[documento]"),
        ("Llámenme al +57 315 564 6977", "+57 315 564 6977", "[telefono]"),
        ("Mi celular 315 564 6977", "315 564 6977", "[telefono]"),
        ("Meu telefone: (11) 91234-5678", "91234-5678", "[telefono]"),
        ("Nací el 18/06/1990", "18/06/1990", "[fecha de nacimiento]"),
        ("Data de nascimento: 01-02-1985", "01-02-1985", "[fecha de nacimiento]"),
    ],
)
def test_personal_data_is_scrubbed(message: str, removed: str, kept: str) -> None:
    scrubbed = scrub_message(message)
    assert removed not in scrubbed
    assert kept in scrubbed


@pytest.mark.parametrize(
    "message",
    [
        "Me cobraron 40000000 ARS en una compra",
        "El cargo de 1500000 COP en MERCADO LIBRE del 15/06/2026",
        "Fui cobrado duas vezes R$ 120,00 em 16/06/2026",
        "El monto fue USD 1,500.00 y la tarjeta termina en 4821",
        "No reconozco la transacción TRX-T1-PURCHASE",
        "me cobraron COP 1.250.000,00 el 17/06/2026 en EXITO",
        "foi cobrado USD 1.250,50 em 03/06/2026",
        "ARS 45.999.999,99",
        "Pagué ARS 45.999.999,99 con la tarjeta ****4821 el 01/06/2026",
    ],
)
def test_transaction_details_are_kept(message: str) -> None:
    assert scrub_message(message) == message


def test_context_payload_uses_only_allowed_fields() -> None:
    payload = context_payload(context())
    assert tuple(payload) == ALLOWED_CONTEXT_FIELDS
    assert payload["language"] == "es"
    assert payload["pending_slot"] is None


def test_context_payload_with_empty_lists() -> None:
    payload = context_payload(LLMContext(customer_ref="CUS-1"))
    assert payload == {
        "customer_ref": "CUS-1",
        "language": None,
        "masked_products": [],
        "transactions": [],
        "pending_slot": None,
        "shown_candidates": [],
        "business_date": None,
    }


def test_context_payload_tolerates_constructed_objects_without_fields() -> None:
    payload = context_payload(LLMContext.model_construct(customer_ref="CUS-1"))
    assert payload["transactions"] == []
    assert payload["masked_products"] == []
