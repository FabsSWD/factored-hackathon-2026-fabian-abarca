"""Deterministic backup for the interrupt signals of ESC-05 and ESC-06.

``human_requested`` and ``legal_or_vulnerability`` must never depend on the language model
alone: the final signal is the union of these rules and the model, and when the model fails
the rules still reach the Policy Engine (see the LLMAdapter contract in ``app.interfaces``).

- ``human_requested`` matches only requests in the present or future addressed to the
  assistant ("quiero hablar con una persona", "pásame con un agente", "quero falar com um
  atendente"). Past narratives ("hablé con un agente ayer", "el agente del comercio me dijo")
  do not match.
- ``legal_or_vulnerability`` matches legal action, a lawyer, a regulator, the media, and
  statements of serious hardship. Policy ESC-06 counts a mention of a lawyer or a regulator
  itself, so these nouns match in any tense.
"""

from __future__ import annotations

import re

from app.contracts import ConversationFlags
from app.input_guard.patterns import normalize


def _any(*words: str) -> str:
    return "(?:" + "|".join(words) + ")"


_PERSON_ES = _any("persona", "persona real", "humano", "humana", "agente", "asesor", "asesora",
                  "ejecutivo", "ejecutiva", "operador", "operadora", "representante",
                  "alguien real", "alguien de verdad", "funcionario", "funcionaria")  # fmt: skip
_PERSON_PT = _any("pessoa", "pessoa de verdade", "humano", "humana", "atendente", "agente",
                  "operador", "operadora", "representante", "alguem de verdade",
                  "funcionario", "funcionaria", "consultor", "consultora")  # fmt: skip
_BOT = _any("robot", "robo", "bot", "maquina", "chatbot", "contestadora", "sistema")

_HUMAN = [
    # Spanish: want/need/can + talk/be attended + person
    rf"\b(?:quiero|quisiera|necesito|deseo|prefiero|me gustaria|puedo|podria|pueden|puede"
    rf"|podrian|dejame|dejeme)\b(?: \w+){{0,3}} (?:hablar|comunicarme|conversar|que me atienda"
    rf"|que me comuniquen|ser atendid[oa]|que me pasen|que me transfieran)\b"
    rf"(?: \w+){{0,4}} {_PERSON_ES}\b",
    rf"\b(?:pasame|paseme|pasenme|comunicame|comuniqueme|comuniquenme|transfiereme"
    rf"|transfierame|transfieranme|conectame|conecteme|comunicarme)\b(?: \w+){{0,3}} {_PERSON_ES}\b",
    rf"\bno quiero (?:hablar|seguir hablando|conversar) con (?:un |una |el |la |este |esta )?{_BOT}\b",
    rf"^(?:un |una |con un |con una )?{_PERSON_ES}(?: por favor| ya| ahora)?$",
    # Portuguese
    rf"\b(?:quero|queria|preciso|gostaria de|posso|poderia|pode|podem|me deixa)\b(?: \w+){{0,3}}"
    rf" (?:falar|conversar|ser atendid[oa]|que me atenda|que me transfiram|que me passem)\b"
    rf"(?: \w+){{0,4}} {_PERSON_PT}\b",
    rf"\bme (?:passa|passe|transfere|transfira|conecta|conecte|coloca|coloque)\b(?: \w+){{0,3}}"
    rf" {_PERSON_PT}\b",
    rf"\bnao quero (?:falar|continuar falando|conversar) com (?:um |uma |o |a |esse |essa )?{_BOT}\b",
    rf"^(?:um |uma |com um |com uma )?{_PERSON_PT}(?: por favor| ja| agora)?$",
]

_LEGAL = [
    # Legal action, lawyers, courts
    r"\b(?:abogad[oa]s?|advogad[oa]s?|mi abogado|meu advogado)\b",
    r"\b(?:voy a|vamos a|los voy a|lo voy a|pienso|tendre que|tengo que|me toca)"
    r"(?: \w+){0,2} (?:demandar|demandarlos|demandarles|iniciar acciones legales|ir a juicio"
    r"|ir a los tribunales|poner una demanda)\b",
    r"\b(?:vou|vamos|pretendo|terei que|tenho que)(?: \w+){0,2} (?:processar|processa-los"
    r"|entrar na justica|abrir um processo|mover uma acao)\b",
    r"\b(?:accion legal|acciones legales|demanda judicial|juicio|tribunal|tutela"
    r"|acao judicial|processo judicial|juizado|pequenas causas)\b",
    # Regulators and consumer protection
    r"\b(?:condusef|profeco|superintendencia financiera|superfinanciera|sfc|bcra|banco central"
    r"|defensa del consumidor|defensoria del consumidor|procon|bacen|reclame aqui"
    r"|consumidor gov|consumidor\.gov)\b",
    # Media
    r"\b(?:periodistas?|prensa|noticiero|medios de comunicacion|jornalistas?|imprensa"
    r"|jornal|televisao|television|lo voy a publicar|vou postar|voy a publicar)\b",
    # Serious hardship or distress
    r"\b(?:estoy desesperad[oa]|estou desesperad[oa]|no tengo (?:dinero|plata) para"
    r"|nao tenho dinheiro para|era todo (?:mi|el) dinero|era todo o meu dinheiro"
    r"|no puedo pagar (?:el arriendo|la renta|la comida|mis medicinas)"
    r"|nao consigo pagar (?:o aluguel|a comida|meus remedios)|me quiero morir|quero morrer)\b",
]

_HUMAN_RE = [re.compile(pattern) for pattern in _HUMAN]
_LEGAL_RE = [re.compile(pattern) for pattern in _LEGAL]


class RuleBasedSignalDetector:
    def detect(self, message: str) -> ConversationFlags:
        text = normalize(message)
        return ConversationFlags(
            human_requested=any(pattern.search(text) for pattern in _HUMAN_RE),
            legal_or_vulnerability=any(pattern.search(text) for pattern in _LEGAL_RE),
        )


def merge_flags(model: ConversationFlags, rules: ConversationFlags) -> ConversationFlags:
    """The union: a signal raised by either source reaches the Policy Engine."""
    return ConversationFlags(
        human_requested=model.human_requested or rules.human_requested,
        account_takeover_reported=model.account_takeover_reported
        or rules.account_takeover_reported,
        legal_or_vulnerability=model.legal_or_vulnerability or rules.legal_or_vulnerability,
        authentication_declined=model.authentication_declined or rules.authentication_declined,
    )
