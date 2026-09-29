"""LLM Adapter (architecture §3): the only path to the language model.

- ``extract`` turns one customer message into candidate slots, flags and the detected
  language (ExtractionResult). The answer is validated against the contract; values outside
  the schema are rejected and retried within the bounds. The interrupt signals of ESC-05 and
  ESC-06 are the union of the model and ``RuleBasedSignalDetector``. When the model gives no
  usable answer, ``ExtractionUnavailableError`` carries the rule-only result as ``fallback``
  (contract for M12 in ``app.interfaces.LLMAdapter``). A ``transaction_id`` proposed by the
  model (it only sees aliases C1, C2, ...) is kept only if it is the alias of a candidate shown
  in the previous turn, translated to the real ID, or the real ID the customer typed; otherwise
  it is set to null (date, amount and merchant stay) and the
  ModelCall records ``transaction_id_discarded``. It is called only for messages the Input
  Guard did not flag (see ``app.interfaces.InputGuard``).
- ``connect`` returns the committed template text with optional connecting sentences around
  it. The template is inserted by this code, never copied by the model, so it reaches the
  customer unchanged (COM-02). A template in another language than the conversation is a
  programming error and raises ``TemplateLanguageError``. Otherwise it fails closed: a sentence
  with a promise (COM-05), a claim that an action happened (COM-04), a digit, a rule
  identifier, braces, the other language, or excessive length is dropped, and any model
  failure, a spent deadline, or ``LLM_CONNECT_ENABLED=false`` returns the template alone.

Both calls share the turn deadline passed by the Orchestrator. Every attempt is recorded as a
ModelCall through the injected recorder (M11 stores them).
"""

from __future__ import annotations

import json
import re
import unicodedata
from datetime import date
from decimal import Decimal
from typing import Any

from pydantic import ValidationError

from app.contracts import (
    ConversationFlags,
    ExtractionResult,
    Language,
    LLMContext,
    Slots,
    TransactionRef,
)
from app.deadline import Deadline
from app.llm_adapter import prompts
from app.llm_adapter.client import MIN_ATTEMPT_SECONDS, Adjusted, LLMError, OpenAIJsonClient
from app.llm_adapter.dates import parse_date_parts, resolve_date
from app.llm_adapter.language import guess_language
from app.llm_adapter.minimization import (
    ALIAS_PREFIX,
    candidate_aliases,
    context_payload,
    scrub_message,
)
from app.llm_adapter.signals import RuleBasedSignalDetector, merge_flags
from app.templates.promises import find_promises

EXTRACT_PURPOSE = "extract_slots"
TRANSACTION_ID_DISCARDED = "transaction_id_discarded"
TRANSACTION_DATE_DISCARDED = "transaction_date_discarded"
CONNECT_PURPOSE = "connect_sentences"
MAX_CLAIMS = 5
MAX_CLAIM_CHARS = 200
MAX_CONNECTING_CHARS = 160
EXTRACT_MAX_TOKENS = 800
CONNECT_MAX_TOKENS = 150

_BOOLEAN_SLOTS = ("card_in_possession", "shared_credentials", "merchant_contacted")
_RULE_ID = re.compile(r"\b(?:GATE|ESC|ACT|COM|DATA)-\d{2}\b|\bRC_[A-Z_]+|\bT[123]\b")
# Actions are only ever reported by templates, after read-back verification (COM-04).
_ACTION_CLAIM = re.compile(
    r"\b(?:registre|registramos|he registrado|hemos registrado|bloquee|bloqueamos"
    r"|he bloqueado|hemos bloqueado|transferi|transferimos|he transferido|hemos transferido"
    r"|cree el caso|creamos el caso|abri (?:un|el|su) caso|abrimos (?:un|el|su) caso"
    r"|registrei|bloqueei|criei o caso|criamos o caso|abri um caso|abrimos um caso"
    r"|(?:ya|ja) (?:quedo|esta|fue|foi|esta|ficou) (?:registrad|bloquead|transferid|cread"
    r"|criad|abiert|abert)\w*)\b"
)


class ExtractionUnavailableError(LLMError):
    """``extract`` got no usable answer. ``fallback`` has empty slots and the rule signals."""

    def __init__(self, message: str, fallback: ExtractionResult) -> None:
        super().__init__(message)
        self.fallback = fallback


class TemplateLanguageError(ValueError):
    """``connect`` received a template in another language than the conversation."""


class OpenAILLMAdapter:
    def __init__(
        self,
        client: OpenAIJsonClient,
        detector: RuleBasedSignalDetector | None = None,
        connect_enabled: bool = True,
    ) -> None:
        self._client = client
        self._detector = detector or RuleBasedSignalDetector()
        self._connect_enabled = connect_enabled

    def new_deadline(self) -> Deadline:
        """The turn deadline to pass to ``extract`` and then ``connect``."""
        return self._client.new_deadline()

    async def extract(
        self, message: str, context: LLMContext, deadline: Deadline | None = None
    ) -> ExtractionResult:
        rule_flags = self._detector.detect(message)
        user = json.dumps(
            {"context": context_payload(context), "message": scrub_message(message)},
            ensure_ascii=False,
        )
        try:
            completion = await self._client.complete_json(
                purpose=EXTRACT_PURPOSE,
                prompt_version=prompts.EXTRACT_PROMPT_VERSION,
                system=prompts.EXTRACT_SYSTEM,
                user=user,
                schema_name="dispute_extraction",
                schema=prompts.EXTRACT_SCHEMA,
                max_output_tokens=EXTRACT_MAX_TOKENS,
                validate=lambda data: _validated(data, message, context),
                deadline=deadline,
            )
        except LLMError as exc:
            raise ExtractionUnavailableError(str(exc), ExtractionResult(flags=rule_flags)) from exc
        result: ExtractionResult = completion.value
        return result.model_copy(update={"flags": merge_flags(result.flags, rule_flags)})

    async def connect(
        self,
        templated_text: str,
        message: str,
        context: LLMContext,
        deadline: Deadline | None = None,
    ) -> str:
        language = context.language or Language.ES
        template_language = guess_language(templated_text)
        if template_language is not None and template_language is not language:
            raise TemplateLanguageError(
                f"template in {template_language.value} for a {language.value} conversation"
            )
        if not self._connect_enabled:
            return templated_text
        if deadline is not None and deadline.remaining() < MIN_ATTEMPT_SECONDS:
            return templated_text
        user = json.dumps(
            {
                "language": language.value,
                "customer_message": scrub_message(message),
                "fixed_message": templated_text,
            },
            ensure_ascii=False,
        )
        try:
            completion = await self._client.complete_json(
                purpose=CONNECT_PURPOSE,
                prompt_version=prompts.CONNECT_PROMPT_VERSION,
                system=prompts.CONNECT_SYSTEM,
                user=user,
                schema_name="connecting_sentences",
                schema=prompts.CONNECT_SCHEMA,
                max_output_tokens=CONNECT_MAX_TOKENS,
                validate=_parse_connecting,
                deadline=deadline,
            )
        except LLMError:
            return templated_text
        before, after = completion.value
        parts = [
            _safe_sentence(before, language),
            templated_text,
            _safe_sentence(after, language),
        ]
        return " ".join(part for part in parts if part)


def _validated(data: dict[str, Any], message: str, context: LLMContext) -> Adjusted:
    aliases = candidate_aliases(context)
    shown = {aliases[ref]: ref for ref in context.shown_candidates if ref in aliases}
    parsed, date_adjustments = parse_extraction(data, context.business_date)
    result, id_adjustments = enforce_transaction_id(parsed, message, shown)
    return Adjusted(result, date_adjustments + id_adjustments)


def enforce_transaction_id(
    result: ExtractionResult, message: str, shown_aliases: dict[str, str]
) -> tuple[ExtractionResult, tuple[str, ...]]:
    """Resolve the model's transaction_id, or discard it.

    The model only ever sees aliases (C1, C2, ...). Its answer is kept only if it is the alias
    of a candidate shown to the customer (translated to the real ID) or the real ID the
    customer typed literally. Anything else is set to null, keeping date, amount and merchant.
    GATE-05 still matches deterministically afterwards (see ``TransactionRef``).
    """
    ref = result.slots.transaction_ref
    if ref is None or ref.transaction_id is None:
        return result, ()
    proposed = ref.transaction_id.strip()
    shown = {alias.upper(): real for alias, real in shown_aliases.items()}
    resolved: str | None = shown.get(proposed.upper())
    is_alias = re.fullmatch(rf"{ALIAS_PREFIX}\d+", proposed, re.I) is not None
    if resolved is None and not is_alias:
        typed = re.search(rf"(?<![\w-]){re.escape(proposed)}(?![\w-])", message, re.I)
        resolved = proposed if typed else None
    if resolved is not None:
        kept_ref = ref.model_copy(update={"transaction_id": resolved})
        slots = result.slots.model_copy(update={"transaction_ref": kept_ref})
        return result.model_copy(update={"slots": slots}), ()
    rest = ref.model_dump(exclude={"transaction_id"}, exclude_none=True)
    kept = TransactionRef.model_validate(rest) if rest else None
    slots = result.slots.model_copy(update={"transaction_ref": kept})
    return result.model_copy(update={"slots": slots}), (TRANSACTION_ID_DISCARDED,)


def parse_extraction(
    data: dict[str, Any], business_date: date | None = None
) -> tuple[ExtractionResult, tuple[str, ...]]:
    """Validate the model's JSON against the contract. Raises ValueError when out of schema.

    Returns the result and the deterministic corrections applied: a transaction date is
    completed and checked in code (``app.llm_adapter.dates``), and set to null with
    ``transaction_date_discarded`` when it cannot be resolved or falls outside the window.
    """
    adjustments: tuple[str, ...] = ()
    # Pydantic's lax mode would read "yes" or 1 as True; the schema allows only JSON booleans.
    slots = dict(data.get("slots") or {})
    flags = data.get("flags") or {}
    booleans = [slots.get(name) for name in _BOOLEAN_SLOTS]
    booleans += [data.get("language_ambiguous", False), *flags.values()]
    if any(value is not None and not isinstance(value, bool) for value in booleans):
        raise ValueError("extraction outside the schema: booleans must be true, false or null")
    ref = slots.get("transaction_ref")
    if isinstance(ref, dict):
        ref = dict(ref)
        parts = parse_date_parts(ref.get("transaction_date"))
        ref["transaction_date"] = None
        if parts is not None:
            resolved = resolve_date(*parts, business_date)
            if resolved is None:
                adjustments = (TRANSACTION_DATE_DISCARDED,)
            ref["transaction_date"] = resolved
        cleaned = {key: value for key, value in ref.items() if value not in (None, "")}
        slots["transaction_ref"] = (
            TransactionRef.model_validate(_decimals(cleaned)) if cleaned else None
        )
    slots = _decimals(slots)
    claims = [str(claim).strip()[:MAX_CLAIM_CHARS] for claim in data.get("customer_claims") or []]
    try:
        result = ExtractionResult(
            detected_language=data.get("detected_language"),
            language_ambiguous=data.get("language_ambiguous", False),
            slots=Slots.model_validate(slots),
            flags=ConversationFlags.model_validate(flags),
            customer_claims=[claim for claim in claims if claim][:MAX_CLAIMS],
        )
        return result, adjustments
    except ValidationError as exc:
        raise ValueError(f"extraction outside the schema: {exc.error_count()} error(s)") from exc


def _decimals(values: dict[str, Any]) -> dict[str, Any]:
    # Amounts arrive as JSON numbers; go through str so 12.5 stays exactly 12.5.
    return {
        key: Decimal(str(value))
        if isinstance(value, float | int)
        and not isinstance(value, bool)
        and key in ("amount", "expected_amount")
        else value
        for key, value in values.items()
    }


def _parse_connecting(data: dict[str, Any]) -> tuple[str, str]:
    before, after = data.get("before"), data.get("after")
    if not isinstance(before, str) or not isinstance(after, str):
        raise ValueError("connecting sentences must be strings")
    return before.strip(), after.strip()


def _fold(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text.lower())
    return "".join(char for char in decomposed if not unicodedata.combining(char))


def _safe_sentence(sentence: str, language: Language) -> str:
    """Fail closed: a doubtful connecting sentence is dropped, never repaired."""
    if not sentence:
        return ""
    sentence_language = guess_language(sentence)
    if (
        len(sentence) > MAX_CONNECTING_CHARS
        or any(char.isdigit() for char in sentence)
        or _RULE_ID.search(sentence)
        or find_promises(sentence)
        or _ACTION_CLAIM.search(_fold(sentence))
        or "{" in sentence
        or "}" in sentence
        or (sentence_language is not None and sentence_language is not language)
    ):
        return ""
    return sentence
