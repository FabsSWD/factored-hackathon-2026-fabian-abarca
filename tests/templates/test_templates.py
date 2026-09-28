from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from app import interfaces
from app.config import load_policy_config
from app.contracts import ActionId, CaseStatus, InformReason, Language, ReasonCode, SlotName
from app.templates.formatting import format_amount, format_date, is_masked, mask_product_number
from app.templates.promises import contains_promise, find_promises
from app.templates.service import (
    CLARIFY_TEMPLATES,
    DEFAULT_TEMPLATES_PATH,
    INFORM_TEMPLATES,
    TemplateError,
    TemplateService,
)

RAW: dict[str, Any] = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))

SAMPLE_VALUES: dict[str, object] = {
    "case_ref": "DSP-20260618-000001",
    "status": "abierta",
    "transaction_date": "16/06/2026",
    "merchant": "Cafe Sintetico",
    "amount": "50.00 USD",
    "product": "****4821",
    "reason": "Cargo duplicado",
    "action": "Registrar una disputa",
    "index": 1,
    "currency": "USD",
}


@pytest.fixture(scope="module")
def templates() -> TemplateService:
    return TemplateService.from_policy(load_policy_config().parameters)


def values_for(service: TemplateService, template_id: str) -> dict[str, object]:
    return {
        name: SAMPLE_VALUES[name] for name in service.placeholders(template_id) if name != "days"
    }


# --- Catalogue ------------------------------------------------------------------------

MILESTONE_TEMPLATES = {
    "case_created",
    "confirm_summary",
    "pending_transaction",
    "declined_transaction",
    "reversed_transaction",
    "outside_window",
    "not_disputable",
    "duplicate_case",
    "handoff",
    "refuse",
    "ask_language",
    "ask_authentication",
    "tool_failure",
}


def test_file_loads_with_version(templates: TemplateService) -> None:
    assert templates.version == "1.0.0"
    assert isinstance(templates, interfaces.TemplateService)


def test_milestone_templates_exist(templates: TemplateService) -> None:
    assert MILESTONE_TEMPLATES <= templates.template_ids


def test_every_inform_reason_has_a_template(templates: TemplateService) -> None:
    assert set(INFORM_TEMPLATES) == set(InformReason)
    assert set(INFORM_TEMPLATES.values()) <= templates.template_ids


def test_every_slot_has_a_clarification(templates: TemplateService) -> None:
    assert set(CLARIFY_TEMPLATES) == set(SlotName)
    assert set(CLARIFY_TEMPLATES.values()) <= templates.template_ids


@pytest.mark.parametrize("template_id", sorted(RAW["templates"]))
def test_every_template_has_es_and_pt_with_the_same_placeholders(template_id: str) -> None:
    entry = RAW["templates"][template_id]
    assert set(entry) == {"es", "pt"}
    names = [set(re.findall(r"\{(\w+)\}", entry[lang])) for lang in ("es", "pt")]
    assert names[0] == names[1]


POLICY_EXAMPLES = [
    (
        "case_created",
        "es",
        "Registramos su disputa con la referencia {case_ref}. Nuestro equipo la revisará en un "
        "plazo de hasta {days} días hábiles y le informaremos el resultado.",
    ),
    (
        "case_created",
        "pt",
        "Registramos sua contestação com a referência {case_ref}. Nossa equipe vai analisá-la em "
        "até {days} dias úteis e informaremos o resultado.",
    ),
    (
        "pending_transaction",
        "es",
        "Esta transacción todavía está pendiente. Podrá disputarla cuando se haya procesado.",
    ),
    (
        "pending_transaction",
        "pt",
        "Esta transação ainda está pendente. Você poderá contestá-la quando ela for processada.",
    ),
    (
        "handoff",
        "es",
        "Voy a transferir su caso a un agente, que ya tendrá la información que me compartió.",
    ),
    (
        "handoff",
        "pt",
        "Vou transferir seu caso para um atendente, que já terá as informações que você me passou.",
    ),
]


@pytest.mark.parametrize(("template_id", "language", "text"), POLICY_EXAMPLES)
def test_policy_section_11_examples_are_verbatim(
    template_id: str, language: str, text: str
) -> None:
    assert RAW["templates"][template_id][language] == text


def test_policy_examples_also_appear_in_the_policy_document() -> None:
    policy = (DEFAULT_TEMPLATES_PATH.parent.parent / "docs" / "dispute-policy.md").read_text(
        encoding="utf-8"
    )
    for _, _, text in POLICY_EXAMPLES:
        assert text in policy


# --- Rendering ------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["es", "pt"])
@pytest.mark.parametrize("template_id", sorted(RAW["templates"]))
def test_every_template_renders_without_unfilled_braces(
    templates: TemplateService, template_id: str, language: str
) -> None:
    text = templates.render(template_id, language, **values_for(templates, template_id))
    assert "{" not in text
    assert "}" not in text


def test_days_come_from_the_policy(templates: TemplateService) -> None:
    text = templates.render("case_created", Language.ES, case_ref="DSP-1")
    assert "hasta 10 días hábiles" in text
    assert "DSP-1" in text
    other = TemplateService(7)
    assert "até 7 dias úteis" in other.render("case_created", "pt", case_ref="DSP-1")


def test_days_cannot_be_overridden(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match="come from the policy"):
        templates.render("case_created", "es", case_ref="DSP-1", days=1)


def test_missing_value_fails_explicitly(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match=r"missing values \['case_ref'\]"):
        templates.render("case_created", "es")


def test_unexpected_value_fails(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match=r"unexpected values \['case_reff'\]"):
        templates.render("case_created", "es", case_ref="DSP-1", case_reff="typo")


def test_unknown_template_fails(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match="unknown template"):
        templates.render("case_approved", "es")


@pytest.mark.parametrize("language", ["en", "ES", "", "es-MX"])
def test_unsupported_language_fails(templates: TemplateService, language: str) -> None:
    with pytest.raises(TemplateError, match="unsupported language"):
        templates.render("handoff", language)


def test_values_are_inserted_literally(templates: TemplateService) -> None:
    text = templates.render("duplicate_case", "es", case_ref="{status}", status="abierta")
    assert "referencia {status}" in text


def test_confirm_summary_with_labels(templates: TemplateService) -> None:
    text = templates.render(
        "confirm_summary",
        "pt",
        transaction_date=format_date(date(2026, 6, 16)),
        merchant=templates.label("misc", "no_merchant", "pt"),
        amount=format_amount(Decimal("200000"), "COP"),
        product=mask_product_number("4111111111114821"),
        reason=templates.label("reason_code", ReasonCode.DUPLICATE.value, "pt"),
        action=templates.label("action", ActionId.CREATE_CASE.value, "pt"),
    )
    assert "16/06/2026, sem estabelecimento, 200.000,00 COP" in text
    assert "Produto: ****4821" in text
    assert "Cobrança duplicada" in text
    assert "sim, confirmo" in text


def test_inform_and_clarify_helpers(templates: TemplateService) -> None:
    assert "pendiente" in templates.inform(InformReason.TRANSACTION_PENDING, "es")
    assert "DSP-9" in templates.inform(
        InformReason.DUPLICATE_CASE, "pt", case_ref="DSP-9", status="aberta"
    )
    assert "tarjeta" in templates.clarify(SlotName.CARD_IN_POSSESSION, "es")
    assert "USD" in templates.clarify(SlotName.EXPECTED_AMOUNT, "pt", currency="USD")


# --- Masking (COM-06) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("number", "masked"),
    [
        ("4111111111114821", "****4821"),
        ("4332181960", "****1960"),
        ("4111 1111 1111 4821", "****4821"),
        ("****4821", "****4821"),
        ("4821", "****4821"),
    ],
)
def test_mask_product_number(number: str, masked: str) -> None:
    assert mask_product_number(number) == masked
    assert is_masked(masked)


def test_mask_needs_four_digits() -> None:
    with pytest.raises(ValueError, match="four digits"):
        mask_product_number("12a")


@pytest.mark.parametrize("value", ["4111111111114821", "4821", "***4821", "****48211", "card"])
def test_full_or_malformed_product_numbers_are_never_rendered(
    templates: TemplateService, value: str
) -> None:
    with pytest.raises(TemplateError, match="masked product number"):
        templates.render("card_blocked", "es", product=value)


# --- Formatting -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "currency", "text"),
    [
        (Decimal("1250.5"), "USD", "1,250.50 USD"),
        (Decimal("1250.5"), "COP", "1.250,50 COP"),
        (Decimal("40000000"), "ARS", "40.000.000,00 ARS"),
        (Decimal("0.005"), "usd", "0.01 USD"),
        (Decimal("18.9"), "BRL", "18,90 BRL"),
    ],
)
def test_format_amount(amount: Decimal, currency: str, text: str) -> None:
    assert format_amount(amount, currency) == text


def test_format_date() -> None:
    assert format_date(date(2026, 6, 5)) == "05/06/2026"
    assert format_date(datetime(2026, 6, 18, 3, 0)) == "18/06/2026"


# --- Labels ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["es", "pt"])
def test_every_label_exists(templates: TemplateService, language: str) -> None:
    for code in ReasonCode:
        assert templates.label("reason_code", code.value, language)
    for status in CaseStatus:
        assert templates.label("case_status", status.value, language)
    for action in (ActionId.CREATE_CASE, ActionId.BLOCK_CARD):
        assert templates.label("action", action.value, language)


def test_unknown_label_fails(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match="unknown label"):
        templates.label("reason_code", "RC_OTHER", "es")


# --- COM-05 and COM-07 ------------------------------------------------------------------


def _all_texts() -> list[str]:
    texts = [text for entry in RAW["templates"].values() for text in entry.values()]
    texts += [
        text
        for kind in RAW["labels"].values()
        for entry in kind.values()
        for text in entry.values()
    ]
    return texts


def test_no_template_promises_a_refund_or_result() -> None:
    for text in _all_texts():
        assert find_promises(text) == [], text


def test_no_template_shows_rule_identifiers_or_thresholds() -> None:
    # COM-07: rule identifiers appear only in the audit record.
    pattern = re.compile(r"\b(?:GATE|ESC|ACT|COM|DATA)-\d{2}\b|\bRC_[A-Z_]+|\bT[123]\b")
    for text in _all_texts():
        assert not pattern.search(text), text


@pytest.mark.parametrize(
    "text",
    [
        "Le reembolsaremos el monto en 5 días.",
        "Se le devolverá el dinero.",
        "Usted recibirá un reembolso completo.",
        "Su dinero será devuelto pronto.",
        "Vamos a acreditarle el monto.",
        "Le garantizamos que se resolverá a su favor.",
        "Su disputa será aprobada.",
        "Tiene un crédito provisional.",
        "Vamos estornar a cobrança.",
        "Você vai receber o estorno amanhã.",
        "O valor será reembolsado.",
        "Garantimos que tudo será resolvido a seu favor.",
        "Sua contestação vai ser aprovada.",
        "Você terá crédito provisório.",
        "LE REEMBOLSAREMOS",
        "Você receberá o dinheiro de volta.",
    ],
)
def test_promises_are_detected(text: str) -> None:
    assert contains_promise(text)


@pytest.mark.parametrize(
    "text",
    [
        "Esta transacción ya fue reversada.",
        "Esta transação já foi estornada.",
        "Le informaremos el resultado.",
        "Su tarjeta de crédito termina en 4821.",
        "O cartão de crédito foi bloqueado.",
        "Nuestro equipo revisará su caso.",
    ],
)
def test_neutral_facts_are_not_promises(text: str) -> None:
    assert not contains_promise(text)


# --- Validation of the file ---------------------------------------------------------------


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "templates.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _with_template(tmp_path: Path, template_id: str, entry: Any) -> Path:
    data = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))
    data["templates"][template_id] = entry
    return _write(tmp_path, data)


@pytest.mark.parametrize(
    ("entry", "message"),
    [
        ({"es": "Hola"}, "needs exactly the languages"),
        ({"es": "Hola", "pt": "Olá", "en": "Hi"}, "needs exactly the languages"),
        ({"es": "Hola {name}", "pt": "Olá"}, "placeholders differ"),
        ({"es": "Hola {name:>10}", "pt": "Olá {name:>10}"}, "plain name"),
        ({"es": "Hola {user.name}", "pt": "Olá {user.name}"}, "plain name"),
        ({"es": "Hola {0}", "pt": "Olá {0}"}, "plain name"),
        ({"es": "Hola {name!r}", "pt": "Olá {name!r}"}, "plain name"),
        ({"es": "Hola {", "pt": "Olá {"}, "malformed braces"),
        ({"es": "   ", "pt": "Olá"}, "text is empty"),
        ({"es": "Le reembolsaremos.", "pt": "Olá"}, "COM-05"),
        ("just text", "needs exactly the languages"),
    ],
)
def test_invalid_templates_fail_at_load(tmp_path: Path, entry: Any, message: str) -> None:
    with pytest.raises(TemplateError, match=message):
        TemplateService(10, _with_template(tmp_path, "custom", entry))


def test_missing_required_template_fails(tmp_path: Path) -> None:
    data = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))
    del data["templates"]["pending_transaction"]
    with pytest.raises(TemplateError, match="missing templates"):
        TemplateService(10, _write(tmp_path, data))


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["labels"]["reason_code"].pop("RC_FEE"), "labels.reason_code"),
        (lambda d: d["labels"].pop("case_status"), "labels.case_status"),
        (
            lambda d: d["labels"]["misc"].update({"no_merchant": {"es": "{x}", "pt": "{x}"}}),
            "labels take no placeholders",
        ),
        (lambda d: d.pop("labels"), "labels section"),
        (lambda d: d.update({"template_version": 1}), "template_version"),
        (lambda d: d.update({"template_version": "1.0"}), "template_version"),
        (lambda d: d.update({"templates": {}}), "templates section"),
    ],
)
def test_invalid_file_structure_fails(tmp_path: Path, change: Any, message: str) -> None:
    data = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))
    change(data)
    with pytest.raises(TemplateError, match=message):
        TemplateService(10, _write(tmp_path, data))


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(TemplateError, match="not found"):
        TemplateService(10, tmp_path / "absent.yaml")


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "templates.yaml"
    path.write_text("templates: [unclosed", encoding="utf-8")
    with pytest.raises(TemplateError, match="not valid YAML"):
        TemplateService(10, path)


def test_non_mapping_file_fails(tmp_path: Path) -> None:
    path = tmp_path / "templates.yaml"
    path.write_text("- a\n", encoding="utf-8")
    with pytest.raises(TemplateError, match="must contain a mapping"):
        TemplateService(10, path)
