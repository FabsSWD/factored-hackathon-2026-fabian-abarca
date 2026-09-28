"""Versioned customer commitments (COM-02), loaded from ``config/templates.yaml``.

The file is validated when it is loaded, so a broken template stops the application instead
of reaching a customer. ``render`` fails loudly on a missing or unknown value and never
returns text with an unfilled placeholder.

Composition rules for the Orchestrator (M12):

- Some templates never close a turn alone; ``REQUIRED_FOLLOW_UPS`` lists the templates one of
  which must follow them. ``tool_failure`` states that an action could not be verified, so it
  is always followed by ``handoff`` (the case escalates under ESC-10) or ``offer_transfer``.
- Every INFORM outcome explains why and offers a human transfer (policy §9): the INFORM
  template is followed by ``offer_transfer``.
- ``handoff_unauthenticated`` is the only handoff text allowed before GATE-02 passes
  (ESC-05 or ESC-06 without a session); it mentions no account data.
- One confirmation turn covers exactly one action (COM-03): ``CONFIRMATION_TEMPLATES`` maps
  each confirmation template to its single action, and ``CONFIRMATION_ORDER`` fixes the order
  when both apply: the card block first (protective and urgent), then the dispute summary.
  Declining the block does not affect the dispute; the flow continues to the summary.
"""

from __future__ import annotations

import string
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.config import PolicyParameters
from app.contracts import ActionId, CaseStatus, InformReason, Language, ReasonCode, SlotName
from app.templates.formatting import FormattedAmount, is_masked
from app.templates.promises import find_promises

DEFAULT_TEMPLATES_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "templates.yaml"

LANGUAGES = tuple(language.value for language in Language)
POLICY_VALUES = frozenset({"days"})
"""Filled from the policy, never by the caller: {days} = RESOLUTION_TARGET_BUSINESS_DAYS."""
MASKED_VALUES = frozenset({"product"})
"""Must receive a masked product number (COM-06)."""
FORMATTED_AMOUNT_VALUES = frozenset({"amount"})
"""Must receive a FormattedAmount from app.templates.formatting.format_amount."""

REQUIRED_FOLLOW_UPS: dict[str, frozenset[str]] = {
    "tool_failure": frozenset({"handoff", "offer_transfer"}),
}
"""Template -> templates one of which must follow it in the same reply."""

CONFIRMATION_TEMPLATES: dict[str, ActionId] = {
    "confirm_block_card": ActionId.BLOCK_CARD,
    "confirm_summary": ActionId.CREATE_CASE,
}
"""Each confirmation template confirms exactly one action (COM-03)."""
CONFIRMATION_ORDER: tuple[str, ...] = ("confirm_block_card", "confirm_summary")

INFORM_TEMPLATES: dict[InformReason, str] = {
    InformReason.AUTHENTICATION_DECLINED: "authentication_declined",
    InformReason.AUTHENTICATION_ATTEMPTS_EXCEEDED: "authentication_attempts_exceeded",
    InformReason.TRANSACTION_PENDING: "pending_transaction",
    InformReason.TRANSACTION_DECLINED: "declined_transaction",
    InformReason.TRANSACTION_REVERSED: "reversed_transaction",
    InformReason.NOT_DISPUTABLE: "not_disputable",
    InformReason.OUTSIDE_WINDOW: "outside_window",
    InformReason.DUPLICATE_CASE: "duplicate_case",
    InformReason.AMOUNT_NOT_EXCEEDED: "amount_not_exceeded",
    InformReason.DELIVERY_DATE_NOT_REACHED: "delivery_date_not_reached",
    InformReason.MERCHANT_NOT_CONTACTED: "merchant_not_contacted",
}
CLARIFY_TEMPLATES: dict[SlotName, str] = {slot: f"clarify_{slot.value}" for slot in SlotName}
CLARIFY_TEMPLATES[SlotName.CONFIRMATION] = "clarify_confirmation"

LABEL_KINDS: dict[str, frozenset[str]] = {
    "reason_code": frozenset(code.value for code in ReasonCode),
    "action": frozenset({ActionId.CREATE_CASE.value, ActionId.BLOCK_CARD.value}),
    "case_status": frozenset(status.value for status in CaseStatus),
    "misc": frozenset({"no_merchant"}),
}


class TemplateError(ValueError):
    """A template is missing, malformed, or rendered with the wrong values."""


@dataclass(frozen=True)
class _Template:
    texts: dict[str, str]
    placeholders: frozenset[str]


def _placeholders(template_id: str, language: str, text: str) -> frozenset[str]:
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(text))
    except ValueError as exc:
        raise TemplateError(f"{template_id}.{language}: malformed braces ({exc})") from exc
    for _, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field.isidentifier() or spec or conversion:
            raise TemplateError(
                f"{template_id}.{language}: placeholder {{{field}}} must be a plain name"
            )
        names.add(field)
    return frozenset(names)


def _texts(where: str, raw: Any) -> dict[str, str]:
    if not isinstance(raw, dict) or set(raw) != set(LANGUAGES):
        raise TemplateError(f"{where}: needs exactly the languages {list(LANGUAGES)}")
    texts: dict[str, str] = {}
    for language in LANGUAGES:
        text = raw[language]
        if not isinstance(text, str) or not text.strip():
            raise TemplateError(f"{where}.{language}: text is empty")
        texts[language] = text
    return texts


class TemplateService:
    def __init__(self, resolution_days: int, path: Path | str | None = None) -> None:
        self._path = Path(path) if path is not None else DEFAULT_TEMPLATES_PATH
        self._days = resolution_days
        raw = self._read()
        self.version: str = self._version(raw)
        self._templates = self._load_templates(raw.get("templates"))
        self._labels = self._load_labels(raw.get("labels"))
        self._check_single_action_confirmations()

    @classmethod
    def from_policy(
        cls, policy: PolicyParameters, path: Path | str | None = None
    ) -> TemplateService:
        """{days} comes from RESOLUTION_TARGET_BUSINESS_DAYS (COM-05)."""
        return cls(policy.RESOLUTION_TARGET_BUSINESS_DAYS, path)

    # --- Public API -------------------------------------------------------------

    @property
    def template_ids(self) -> frozenset[str]:
        return frozenset(self._templates)

    def placeholders(self, template_id: str) -> frozenset[str]:
        return self._get(template_id).placeholders

    def render(self, template_id: str, language: Language | str, **values: object) -> str:
        template = self._get(template_id)
        lang = self._language(language)
        overridden = POLICY_VALUES & set(values)
        if overridden:
            raise TemplateError(f"{template_id}: {sorted(overridden)} come from the policy")
        values = {**values, **{"days": self._days}} if "days" in template.placeholders else values
        missing = template.placeholders - set(values)
        if missing:
            raise TemplateError(f"{template_id}: missing values {sorted(missing)}")
        unknown = set(values) - template.placeholders
        if unknown:
            raise TemplateError(f"{template_id}: unexpected values {sorted(unknown)}")
        for name in MASKED_VALUES & template.placeholders:
            if not is_masked(str(values[name])):
                raise TemplateError(f"{template_id}: {{{name}}} must be a masked product number")
        for name in FORMATTED_AMOUNT_VALUES & template.placeholders:
            if not isinstance(values[name], FormattedAmount):
                raise TemplateError(f"{template_id}: {{{name}}} must come from format_amount")
        return template.texts[lang].format_map({key: str(value) for key, value in values.items()})

    def label(self, kind: str, key: str, language: Language | str) -> str:
        lang = self._language(language)
        try:
            return self._labels[kind][key][lang]
        except KeyError:
            raise TemplateError(f"unknown label {kind}.{key}") from None

    def inform(self, reason: InformReason, language: Language | str, **values: object) -> str:
        return self.render(INFORM_TEMPLATES[reason], language, **values)

    def clarify(self, slot: SlotName, language: Language | str, **values: object) -> str:
        return self.render(CLARIFY_TEMPLATES[slot], language, **values)

    # --- Loading ------------------------------------------------------------------

    def _read(self) -> dict[str, Any]:
        try:
            raw = yaml.safe_load(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise TemplateError(f"Templates file not found: {self._path}") from exc
        except yaml.YAMLError as exc:
            raise TemplateError(f"Templates file is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise TemplateError("Templates file must contain a mapping")
        return raw

    @staticmethod
    def _version(raw: dict[str, Any]) -> str:
        version = raw.get("template_version")
        if not isinstance(version, str) or not version.count(".") == 2:
            raise TemplateError("template_version must be a string like 1.0.0")
        return version

    def _load_templates(self, raw: Any) -> dict[str, _Template]:
        if not isinstance(raw, dict) or not raw:
            raise TemplateError("templates section is missing or empty")
        templates: dict[str, _Template] = {}
        for template_id, entry in raw.items():
            texts = _texts(str(template_id), entry)
            per_language = {
                language: _placeholders(template_id, language, text)
                for language, text in texts.items()
            }
            if len(set(per_language.values())) != 1:
                raise TemplateError(
                    f"{template_id}: placeholders differ between languages {per_language}"
                )
            for language, text in texts.items():
                promises = find_promises(text)
                if promises:
                    raise TemplateError(
                        f"{template_id}.{language}: promises a refund or result {promises} (COM-05)"
                    )
            templates[str(template_id)] = _Template(texts, per_language[LANGUAGES[0]])
        required = (
            set(INFORM_TEMPLATES.values())
            | set(CLARIFY_TEMPLATES.values())
            | set(REQUIRED_FOLLOW_UPS)
            | {follow_up for options in REQUIRED_FOLLOW_UPS.values() for follow_up in options}
            | {"handoff_unauthenticated", "session_expired_reconfirm"}
            | set(CONFIRMATION_TEMPLATES)
        )
        missing = required - set(templates)
        if missing:
            raise TemplateError(f"missing templates {sorted(missing)}")
        return templates

    @staticmethod
    def _load_labels(raw: Any) -> dict[str, dict[str, dict[str, str]]]:
        if not isinstance(raw, dict):
            raise TemplateError("labels section is missing")
        labels: dict[str, dict[str, dict[str, str]]] = {}
        for kind, expected in LABEL_KINDS.items():
            entries = raw.get(kind)
            if not isinstance(entries, dict) or set(entries) != expected:
                raise TemplateError(f"labels.{kind} must define exactly {sorted(expected)}")
            labels[kind] = {
                str(key): _texts(f"labels.{kind}.{key}", value) for key, value in entries.items()
            }
            for key, texts in labels[kind].items():
                for language, text in texts.items():
                    if _placeholders(f"labels.{kind}.{key}", language, text):
                        raise TemplateError(f"labels.{kind}.{key}: labels take no placeholders")
        return labels

    def _check_single_action_confirmations(self) -> None:
        """A confirmation names only its own action: no {action} slot, no other action's label."""
        for template_id, action in CONFIRMATION_TEMPLATES.items():
            template = self._get(template_id)
            if "action" in template.placeholders:
                raise TemplateError(f"{template_id}: confirms one fixed action, no {{action}}")
            others = [a for a in (ActionId.CREATE_CASE, ActionId.BLOCK_CARD) if a is not action]
            for language, text in template.texts.items():
                for other in others:
                    if self._labels["action"][other.value][language] in text:
                        raise TemplateError(
                            f"{template_id}.{language}: lists {other.value}; "
                            "one confirmation covers exactly one action (COM-03)"
                        )

    # --- Helpers --------------------------------------------------------------------

    def _get(self, template_id: str) -> _Template:
        try:
            return self._templates[template_id]
        except KeyError:
            raise TemplateError(f"unknown template {template_id}") from None

    @staticmethod
    def _language(language: Language | str) -> str:
        value = language.value if isinstance(language, Language) else language
        if value not in LANGUAGES:
            raise TemplateError(f"unsupported language {language!r}")
        return value
