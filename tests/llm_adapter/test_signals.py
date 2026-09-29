from __future__ import annotations

import pytest

from app.contracts import ConversationFlags
from app.llm_adapter.signals import RuleBasedSignalDetector, merge_flags

detector = RuleBasedSignalDetector()

HUMAN_REQUESTS = [
    "Quiero hablar con una persona",
    "Quisiera hablar con un agente, por favor",
    "Necesito que me atienda un humano",
    "Pásame con un agente",
    "Comuníqueme con un asesor",
    "No quiero hablar con un robot",
    "Un humano por favor",
    "¿Puedo hablar con alguien real?",
    "Quero falar com um atendente",
    "Queria falar com uma pessoa",
    "Me passa para um atendente, por favor",
    "Não quero falar com um robô",
    "Um atendente, por favor",
]

LEGAL_OR_VULNERABILITY = [
    "Voy a demandar al banco",
    "Mi abogado dice que esto es ilegal",
    "Voy a poner la queja en la Condusef",
    "Lo voy a publicar en la prensa",
    "Estoy desesperado, era todo mi dinero",
    "No tengo dinero para comer por este cobro",
    "Vou processar vocês",
    "Meu advogado vai entrar em contato",
    "Vou reclamar no Procon",
    "Estou desesperada, era todo o meu dinheiro",
]

# Past narratives and ordinary requests: not an interrupt.
NOT_SIGNALS = [
    "Hablé con un agente ayer y no me ayudó",
    "El agente del comercio me dijo que llamara",
    "Ya hablé con la persona de la tienda",
    "La asesora de la sucursal me explicó el cargo",
    "Quiero disputar un cargo",
    "Necesito hablar del cargo que no reconozco",
    "Falei com um atendente ontem",
    "O atendente da loja disse que não podia",
    "A pessoa do banco me explicou a tarifa",
    "Quero contestar uma cobrança",
]


@pytest.mark.parametrize("message", HUMAN_REQUESTS)
def test_human_requests(message: str) -> None:
    flags = detector.detect(message)
    assert flags.human_requested is True
    assert flags.legal_or_vulnerability is False


@pytest.mark.parametrize("message", LEGAL_OR_VULNERABILITY)
def test_legal_or_vulnerability(message: str) -> None:
    flags = detector.detect(message)
    assert flags.legal_or_vulnerability is True
    assert flags.human_requested is False


@pytest.mark.parametrize("message", NOT_SIGNALS)
def test_past_narratives_are_not_interrupts(message: str) -> None:
    assert detector.detect(message) == ConversationFlags()


def test_rules_never_raise_model_only_signals() -> None:
    flags = detector.detect("Me robaron el celular y no quiero identificarme")
    assert flags.account_takeover_reported is False
    assert flags.authentication_declined is False


def test_merge_is_a_union() -> None:
    model = ConversationFlags(account_takeover_reported=True)
    rules = ConversationFlags(human_requested=True)
    assert merge_flags(model, rules) == ConversationFlags(
        human_requested=True, account_takeover_reported=True
    )
    assert merge_flags(ConversationFlags(), ConversationFlags()) == ConversationFlags()
