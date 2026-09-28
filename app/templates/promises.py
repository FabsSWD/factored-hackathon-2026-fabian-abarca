"""COM-05: the system never promises a refund, a credit or a result.

``find_promises`` checks any customer-facing text: the templates when they are loaded, and
(from M12 on) the final reply, including the language model's connecting sentences.

The patterns target commitments ("le reembolsaremos", "vai receber o estorno", "a su favor"),
not neutral facts: "esta transacción ya fue reversada" / "já foi estornada" state what the
records show and are allowed. Matching ignores case and accents.

Fail closed (M12): if a connecting sentence written by the language model is flagged, that
sentence is discarded and the reply is built from the templates alone. The detector is never
used to "fix" a sentence. Its recall on free text is measured in M17.
"""

from __future__ import annotations

import re
import unicodedata

_REFUND_NOUNS_ES = r"(?:reembolso|devolucion|reintegro|abono|credito|dinero de vuelta)"
_REFUND_NOUNS_PT = r"(?:reembolso|estorno|ressarcimento|devolucao|credito|dinheiro de volta)"

_PATTERNS: tuple[tuple[str, str], ...] = (
    # Spanish
    (
        "es",
        r"\b(?:le |les )?(?:reembolsaremos|devolveremos|reintegraremos|abonaremos"
        r"|acreditaremos|regresaremos)\b",
    ),
    ("es", r"\bse le (?:reembolsara|devolvera|reintegrara|abonara|acreditara)\b"),
    ("es", rf"\b(?:recibira|obtendra|tendra|le daremos) (?:un |el |su )?{_REFUND_NOUNS_ES}\b"),
    ("es", r"\b(?:sera|seran) (?:reembolsad|devuelt|reintegrad|abonad|acreditad)[oa]s?\b"),
    ("es", r"\bvamos a (?:reembolsar|devolver|reintegrar|abonar|acreditar)\w*\b"),
    ("es", r"\b(?:garantizamos|garantizado|garantizada|le aseguramos|le prometemos)\b"),
    ("es", r"\ba su favor\b"),
    ("es", r"\b(?:sera aprobad[oa]|se aprobara|aprobaremos)\b"),
    ("es", r"\bcredito provisional\b"),
    # Portuguese
    ("pt", r"\b(?:reembolsaremos|devolveremos|estornaremos|ressarciremos|creditaremos)\b"),
    ("pt", r"\bvamos (?:reembolsar|devolver|estornar|ressarcir|creditar)\b"),
    ("pt", rf"\b(?:recebera|vai receber|tera|voce tera) (?:o |um |seu |sua )?{_REFUND_NOUNS_PT}\b"),
    ("pt", r"\bser(?:a|ao) (?:reembolsad|devolvid|estornad|ressarcid|creditad)[oa]s?\b"),
    ("pt", r"\b(?:garantimos|garantido|garantida|prometemos|com certeza)\b"),
    ("pt", r"\ba seu favor\b"),
    ("pt", r"\b(?:sera aprovad[oa]|vai ser aprovad[oa]|aprovaremos)\b"),
    ("pt", r"\bcredito provisorio\b"),
)

_COMPILED = tuple((language, re.compile(pattern)) for language, pattern in _PATTERNS)


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def find_promises(text: str) -> list[str]:
    """The promise-like phrases found in ``text`` (accent-folded), or an empty list."""
    folded = _fold(text)
    return [match.group(0) for _, pattern in _COMPILED for match in pattern.finditer(folded)]


def contains_promise(text: str) -> bool:
    return bool(find_promises(text))
