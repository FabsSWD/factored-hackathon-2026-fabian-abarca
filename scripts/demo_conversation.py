"""Walk two complete conversations through POST /api/turn with test doubles (no OpenAI, no
Kev, no database): one in Spanish and one in Portuguese, turn by turn.

    python scripts/demo_conversation.py

The real Policy Engine, Handoff Builder and templates run; the LLM answers come from a script
(the extraction a real model would return for each message). Use it to review the replies,
for M13 and for the demo.
"""

from __future__ import annotations

import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402
from pydantic import SecretStr  # noqa: E402

from app.api.dependencies import RateLimits  # noqa: E402
from app.config import load_policy_config  # noqa: E402
from app.contracts import Confirmation, ReasonCode, TransactionRef  # noqa: E402
from app.identity.rate_limit import RateLimiter  # noqa: E402
from app.main import create_app  # noqa: E402
from app.settings import Settings  # noqa: E402
from tests.orchestrator.fakes import TOKEN, World, build_world, ext  # noqa: E402

REF = TransactionRef(transaction_date=date(2026, 6, 10), merchant="Cafe Sintetico")

SPANISH = [
    ("Hola, no reconozco un cargo de Cafe Sintetico del 10 de junio",
     ext(transaction_ref=REF, reason_code=ReasonCode.UNRECOGNIZED,
         claims=["No reconoce el cargo de Cafe Sintetico"])),
    ("No, prefiero no bloquearla por ahora", ext(confirmation=Confirmation.DECLINED)),
    ("Sí, la tarjeta la tengo conmigo", ext(card_in_possession=True)),
    ("No, no le di mis claves a nadie", ext(shared_credentials=False)),
    ("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED)),
]
# The two Streaming Plus charges are listed newest first: TXN-5 (20:00), then TXN-4 (08:00).
PORTUGUESE = [
    ("Fui cobrado duas vezes pela Streaming Plus, 18,90 dólares",
     ext("pt", transaction_ref=TransactionRef(amount=Decimal("18.90"), merchant="Streaming Plus"),
         reason_code=ReasonCode.DUPLICATE, claims=["Cobrado duas vezes pela Streaming Plus"])),
    ("a primeira, a das 20h", ext("pt", transaction_ref=TransactionRef(transaction_id="TXN-5"))),
    ("acho que sim", ext("pt", confirmation=Confirmation.HEDGED)),
    ("sim, é essa", ext("pt", confirmation=Confirmation.CONFIRMED)),
    ("Quero falar com um atendente", ext("pt", flags={"human_requested": True},
                                          claims=["Quer falar com um atendente"])),
]


def run(world: World, client: TestClient, title: str, turns: list) -> None:  # type: ignore[type-arg]
    print(f"\n===== {title} =====")
    conversation_id = None
    for message, extraction in turns:
        world.say(message, extraction)
        body = {"message": message, **({"conversation_id": conversation_id} if conversation_id else {})}
        response = client.post("/api/turn", json=body, headers={"Authorization": f"Bearer {TOKEN}"})
        result = response.json()
        conversation_id = result["conversation_id"]
        trace = world.tracer.traces[-1]
        print(f"\n[turn {result['turn_index']}] customer: {message}")
        print(f"  assistant: {result['reply']}")
        print(f"  (outcome {trace.outcome}, handed_off={result['handed_off']})")
    for packet in world.bank.packets:
        print(f"\n  handoff {packet.handoff_id} -> {packet.queue}/{packet.priority}: {packet.request_summary}")
        print(f"  collected_slots: {[(s.name.value, s.value) for s in packet.collected_slots]}")
    for case in world.bank.cases:
        print(f"\n  case {case.case_id}: {case.reason_code} {case.tier} {case.status}")


def main() -> None:
    settings = Settings(_env_file=None, business_date=date(2026, 6, 17),  # type: ignore[call-arg]
                        pseudonym_key=SecretStr("demo-pseudonym-key"))
    limits = RateLimits(per_session=RateLimiter(1000), auth_per_ip=RateLimiter(1000))
    for title, turns in (("Español: cargo no reconocido", SPANISH),
                         ("Português: cobrança duplicada", PORTUGUESE)):
        world = build_world()
        app = create_app(load_policy_config(), settings=settings, orchestrator=world.orchestrator,
                         rate_limits=limits)
        with TestClient(app) as client:
            run(world, client, title, turns)


if __name__ == "__main__":
    main()
