"""Manual check against the real OpenAI API (M5, Fabs's action). Tests never call it.

    python scripts/llm_smoke.py            # print extractions, replies and model calls
    python scripts/llm_smoke.py --record   # also save the raw answers as parser fixtures

Sends synthetic messages (es, pt) through the LLM Adapter: three first messages and six
replies to a pending question (the COM-03 summary or the RC_DUPLICATE question), with the settings in .env
(OPENAI_API_KEY, LLM_MODEL). The reply template is rendered in the detected language. With
``--record``, the raw JSON answers of ``extract`` go to tests/fixtures/llm/ (synthetic data
only). Uses a few cents of API credit.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_policy_config  # noqa: E402
from app.contracts import (  # noqa: E402
    Language,
    LLMContext,
    LLMTransaction,
    ModelCall,
    SideQuestion,
    SlotName,
)
from app.llm_adapter import prompts  # noqa: E402
from app.llm_adapter.adapter import OpenAILLMAdapter  # noqa: E402
from app.llm_adapter.client import LLMClientConfig, OpenAIJsonClient  # noqa: E402
from app.settings import get_settings  # noqa: E402
from app.templates.service import TemplateService  # noqa: E402

FIXTURES = ROOT / "tests" / "fixtures" / "llm"

CONTEXT = LLMContext(
    customer_ref="CUS-smoke-0001",
    masked_products=["****4821"],
    transactions=[
        LLMTransaction(
            transaction_ref="TRX-SMOKE-1",
            transaction_date=date(2026, 6, 16),
            amount=Decimal("50.00"),
            currency="USD",
            merchant_name="Cafe Sintetico",
            transaction_status="Approved",
        ),
        LLMTransaction(
            transaction_ref="TRX-SMOKE-2",
            transaction_date=date(2026, 6, 15),
            amount=Decimal("18.90"),
            currency="USD",
            merchant_name="Streaming Plus",
            transaction_status="Approved",
        ),
    ],
)

# (fixture name, language, message, slot pending from the previous turn)
MESSAGES: list[tuple[str, Language, str, SlotName | None]] = [
    ("es_unrecognized", Language.ES,
     "Hola, no reconozco un cargo de 50 dólares en Cafe Sintetico del 16 de junio. Tengo mi "
     "tarjeta conmigo y no le he dado mis claves a nadie.", None),
    ("pt_duplicate_human", Language.PT,
     "Fui cobrado duas vezes pela Streaming Plus, 18,90 dólares. Quero falar com um atendente.",
     None),
    ("es_not_received_legal", Language.ES,
     "Pagué unos audífonos que nunca llegaron, debían entregarlos el 2026-06-01 y ya le "
     "escribí a la tienda. Si no se resuelve voy a ir con un abogado.", None),
    # Replies to the COM-03 summary and to the RC_DUPLICATE question (extract@1.6.0).
    ("es_confirm_confirmed", Language.ES, "sí, confirmo", SlotName.CONFIRMATION),
    ("es_confirm_declined_amount", Language.ES, "no, el monto está mal, eran 40",
     SlotName.CONFIRMATION),
    ("es_confirm_withdrawn", Language.ES, "mejor ya no, déjelo así", SlotName.CONFIRMATION),
    ("pt_confirm_withdrawn", Language.PT, "deixa pra lá, não quero mais", SlotName.CONFIRMATION),
    ("es_confirm_hedged", Language.ES, "creo que sí, aunque no estoy seguro",
     SlotName.CONFIRMATION),
    ("es_duplicate_ref_confirmed", Language.ES, "sí, ese es", SlotName.DUPLICATE_REF),
    # Side questions, "ese no es" and a block request (extract@1.7.0); the first two are the
    # customer's words in the manual test of M12.
    ("es_side_refund", Language.ES, "Existe una posibilidad de reembolso?",
     SlotName.CARD_IN_POSSESSION),
    ("es_side_refund_again", Language.ES,
     "Eso no fue lo que pregunté, puedo pedir un reembolso?", SlotName.CONFIRMATION),
    ("es_answer_and_refund", Language.ES, "sí, la tengo, ¿y me devuelven el dinero?",
     SlotName.CARD_IN_POSSESSION),
    ("es_wrong_transaction", Language.ES, "ese no es", SlotName.CONFIRMATION),
    ("es_block_requested", Language.ES, "Mejor sí, bloquéela por favor", SlotName.CONFIRMATION),
    ("pt_side_other", Language.PT, "Vocês oferecem empréstimo pessoal?", None),
    # flow_help, a clearly unrelated question and an approximate amount (extract@1.8.0); the
    # first and the last are the customer's words in manual test 2 of M12.
    ("es_flow_help_name_only", Language.ES,
     "Solo sé el nombre del lugar, si te lo doy me podrías confirmar lo demás?",
     SlotName.TRANSACTION_REF),
    ("es_flow_help_what_data", Language.ES, "¿Qué datos necesitas?", SlotName.TRANSACTION_REF),
    ("es_flow_help_no_amount", Language.ES, "No recuerdo el monto", SlotName.TRANSACTION_REF),
    ("es_side_other_account", Language.ES, "¿Puedo abrir una cuenta de ahorros con ustedes?",
     None),
    ("es_approximate_amount", Language.ES,
     "la transacción del restaurante el buen sabor, fue como de 40 dólares",
     SlotName.TRANSACTION_REF),
    # Periods of days and a generic merchant (extract@1.9.0); the first and the last are the
    # customer's words in manual test 3 of M12.
    ("es_period_15_19", Language.ES, "Si, fue entre el 15 y el 19 de junio",
     SlotName.TRANSACTION_REF),
    ("es_period_mid_june", Language.ES, "fue a mediados de junio", SlotName.TRANSACTION_REF),
    ("es_period_last_week", Language.ES, "fue la semana pasada", SlotName.TRANSACTION_REF),
    ("pt_period_10_12", Language.PT, "foi entre 10 e 12 de junho", SlotName.TRANSACTION_REF),
    ("es_generic_restaurant", Language.ES,
     "el monto era como de 40 dólares, la compra fue en un restaurante pero no recuerdo bien "
     "su nombre", SlotName.TRANSACTION_REF),
    # A stolen card is not account takeover; the words of scenario S013 (extract@1.11.0).
    ("pt_card_stolen", Language.PT, "Não acho, acho que roubaram", SlotName.CARD_IN_POSSESSION),
    # Distinct unrecognized charges for the ESC-03 batch (extract@1.11.0).
    ("es_three_unrecognized", Language.ES,
     "No reconozco tres cargos: uno de 50 dólares en Cafe Sintetico el 16 de junio, otro de 18,90 "
     "dólares en Streaming Plus el 15 de junio y uno de 12 dólares en Farmacia Noche ayer.", None),
    ("es_same_charge_twice", Language.ES,
     "No reconozco el cargo de 50 dólares en Cafe Sintetico. Ese cargo de Cafe Sintetico yo no lo "
     "hice, repito, no reconozco ese cobro de Cafe Sintetico.", None),
    ("pt_no_unrecognized", Language.PT,
     "O valor da compra na Streaming Plus não é o combinado, eram 15 dólares.", None),
]  # fmt: skip


async def main(record: bool) -> None:
    settings = get_settings()
    calls: list[ModelCall] = []
    raw: dict[str, dict[str, Any]] = {}
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    client = OpenAIJsonClient(
        LLMClientConfig(
            api_key=key,
            model=settings.llm_model or "",
            base_url=settings.openai_base_url,
            timeout_seconds=settings.llm_timeout_seconds,
            max_retries=settings.llm_max_retries,
            turn_deadline_seconds=settings.llm_turn_deadline_seconds,
        ),
        recorder=calls.append,
        response_sink=lambda purpose, data: raw.__setitem__(purpose, data),
    )
    adapter = OpenAILLMAdapter(client, connect_enabled=settings.llm_connect_enabled)
    templates = TemplateService.from_policy(load_policy_config().parameters)

    for name, language, message, pending_slot in MESSAGES:
        deadline = adapter.new_deadline()
        context = CONTEXT.model_copy(
            update={
                "language": language,
                "business_date": settings.business_date,
                "pending_slot": pending_slot,
            }
        )
        print(f"\n=== {name} [{language.value}] {message}")
        result = await adapter.extract(message, context, deadline)
        print(result.model_dump_json(indent=2))
        detected = Language(result.detected_language) if result.detected_language in ("es", "pt") else language
        reply_context = context.model_copy(update={"language": detected})
        template = templates.render("handoff", detected)
        if result.side_question is SideQuestion.OTHER:
            # The Orchestrator sends no connecting sentence for a request outside disputes.
            print(f"reply ({detected.value}): no connect for side question other")
        else:
            # connect: full sentences on a first message, brief on a reply.
            brief = pending_slot is not None
            reply = await adapter.connect(template, message, reply_context, deadline, brief=brief)
            print(f"reply ({detected.value}, {'brief' if brief else 'full'}):", reply)
        if record:
            FIXTURES.mkdir(parents=True, exist_ok=True)
            fixture = {
                "prompt_version": prompts.EXTRACT_PROMPT_VERSION,
                "response_model": next(
                    (c.response_model for c in reversed(calls)
                     if c.purpose == "extract_slots" and c.success),
                    None,
                ),
                "language": language.value,
                "pending_slot": pending_slot.value if pending_slot else None,
                "message": message,
                "raw": raw.get("extract_slots"),
            }
            path = FIXTURES / f"{name}.json"
            path.write_text(json.dumps(fixture, indent=2, ensure_ascii=False), encoding="utf-8")
            print(f"recorded {path.relative_to(ROOT)}")

    print("\n=== extract by prompt version (for reports/m5_llm_extraction_evidence.json)")
    extracts = [c for c in calls if c.purpose == "extract_slots" and c.success]
    if extracts:
        tokens = [c.input_tokens or 0 for c in extracts]
        seconds = [c.latency_ms / 1000 for c in extracts]
        print(
            f"{prompts.EXTRACT_PROMPT_VERSION}: input_tokens {min(tokens)}-{max(tokens)}, "
            f"latency {min(seconds):.1f}-{max(seconds):.1f} s over {len(extracts)} calls"
        )

    print("\n=== model calls")
    for call in calls:
        print(
            f"{call.purpose:18} {call.response_model or call.model} {call.prompt_version} "
            f"{call.prompt_hash} fp={call.system_fingerprint} success={call.success} "
            f"in={call.input_tokens} out={call.output_tokens} {call.latency_ms:.0f} ms "
            f"{call.error or ''}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--record", action="store_true", help="save raw answers as fixtures")
    asyncio.run(main(parser.parse_args().record))
