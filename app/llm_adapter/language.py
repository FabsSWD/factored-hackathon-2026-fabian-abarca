"""Tell Spanish from Portuguese in short texts, by distinctive words and characters.

Used to keep a reply in one language (COM-01): ``connect`` drops a connecting sentence in the
other language and refuses a template whose language does not match the conversation. It
returns ``None`` when the text gives no clear signal (for example the bilingual
``ask_language`` template), and callers treat ``None`` as "no mismatch".
"""

from __future__ import annotations

import re
import unicodedata

from app.contracts import Language

_SPANISH_CHARS = re.compile(r"[ñ¿¡]")
_PORTUGUESE_CHARS = re.compile(r"[ãõç]|ões\b")
_WORD = re.compile(r"[a-z]+")

_SPANISH_WORDS = frozenset({
    "usted", "su", "sus", "le", "les", "gracias", "entiendo", "tarjeta", "cuenta", "transaccion",
    "disputa", "disputarla", "agente", "ayudar", "ayuda", "puedo", "puede", "muy", "tambien",
    "ahora", "ya", "una", "un", "lo", "siento", "estoy", "voy", "hay", "pero", "con", "del",
    "el", "los", "las", "y", "que", "comercio", "monto", "cargo", "caso", "si", "haya",
    "lamento", "espero", "podra", "todavia", "fecha", "entrega", "registrar", "transferir",
    "prefiere", "en", "espanol",
})  # fmt: skip
_PORTUGUESE_WORDS = frozenset({
    "voce", "nao", "sim", "obrigado", "obrigada", "seu", "sua", "seus", "suas", "um", "uma",
    "muito", "tambem", "agora", "ja", "isso", "pelo", "pela", "vou", "vai", "estou", "sao",
    "entendo", "informacoes", "atendente", "contestacao", "cartao", "transacao", "conta",
    "preciso", "posso", "ajudar", "ajuda", "com", "do", "da", "dos", "das", "e", "o", "os",
    "ao", "estabelecimento", "valor", "cobranca", "caso", "se", "tenha", "lamento", "espero",
    "podera", "ainda", "data", "entrega", "registrar", "transferir", "voces", "nossa", "nosso",
    "prefere", "em", "espanhol", "ou",
})  # fmt: skip
CLEAR_MARGIN = 1.5
"""The winning language needs this many times the other's evidence; otherwise no signal."""
_SHARED = _SPANISH_WORDS & _PORTUGUESE_WORDS


def _words(text: str) -> list[str]:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(char for char in decomposed if not unicodedata.combining(char))
    return _WORD.findall(stripped)


def guess_language(text: str) -> Language | None:
    lowered = text.lower()
    spanish = 2 * len(_SPANISH_CHARS.findall(lowered))
    portuguese = 2 * len(_PORTUGUESE_CHARS.findall(lowered))
    for word in _words(text):
        if word in _SHARED:
            continue
        spanish += word in _SPANISH_WORDS
        portuguese += word in _PORTUGUESE_WORDS
    if max(spanish, portuguese) < CLEAR_MARGIN * min(spanish, portuguese) or spanish == portuguese:
        return None
    return Language.ES if spanish > portuguese else Language.PT
