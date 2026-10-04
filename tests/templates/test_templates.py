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
from app.contracts import (
    ActionId,
    CaseStatus,
    InformReason,
    Language,
    ReasonCode,
    SideQuestion,
    SlotName,
)
from app.templates.formatting import (
    FormattedAmount,
    Locale,
    currency_said,
    format_amount,
    format_date,
    is_masked,
    locale_for,
    mask_product_number,
    plain_number,
)
from app.templates.promises import contains_promise, find_promises
from app.templates.service import (
    CLARIFY_TEMPLATES,
    CONFIRMATION_ORDER,
    CONFIRMATION_TEMPLATES,
    DEFAULT_TEMPLATES_PATH,
    INFORM_TEMPLATES,
    POLICY_VALUES,
    REQUIRED_FOLLOW_UPS,
    SIDE_TEMPLATES,
    TemplateError,
    TemplateService,
)

RAW: dict[str, Any] = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))

SAMPLE_VALUES: dict[str, object] = {
    "case_ref": "DSP-20260618-000001",
    "handoff_ref": "HO-20260618-000001",
    "status": "abierta",
    "transaction_date": "16/06/2026",
    "merchant": "Cafe Sintetico",
    "amount": format_amount(Decimal("50"), "USD", Locale.ES_MX),
    "product": "****4821",
    "reason": "Cargo duplicado",
    "action": "Registrar una disputa",
    "index": 1,
    "currency": "USD",
    "detail": "la fecha aproximada de la compra",
    "missing": "la fecha aproximada de la compra",
    "known": "en El Buen Sabor por unos USD 40,00",
}


@pytest.fixture(scope="module")
def templates() -> TemplateService:
    return TemplateService.from_policy(load_policy_config().parameters)


def values_for(service: TemplateService, template_id: str) -> dict[str, object]:
    return {
        name: SAMPLE_VALUES[name]
        for name in service.placeholders(template_id)
        if name not in POLICY_VALUES
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
    assert templates.version == "1.14.0"
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
        amount=format_amount(Decimal("200000"), "COP", Locale.PT_BR),
        product=mask_product_number("4111111111114821"),
        reason=templates.label("reason_code", ReasonCode.DUPLICATE.value, "pt"),
    )
    assert "16/06/2026, sem estabelecimento, COP 200.000,00" in text
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
    ("language", "country", "locale"),
    [
        ("es", "México", Locale.ES_MX),
        ("es", "Mexico", Locale.ES_MX),
        ("es", "Colombia", Locale.ES_CO),
        ("es", "Argentina", Locale.ES_AR),
        (Language.ES, " ARGENTINA ", Locale.ES_AR),
        ("es", None, Locale.ES_CO),
        ("es", "Chile", Locale.ES_CO),
        ("pt", "México", Locale.PT_BR),
        (Language.PT, None, Locale.PT_BR),
    ],
)
def test_locale_from_language_and_country(
    language: Language | str, country: str | None, locale: Locale
) -> None:
    assert locale_for(language, country) is locale


def test_locale_rejects_unsupported_language() -> None:
    with pytest.raises(ValueError, match="unsupported language"):
        locale_for("en", "México")


@pytest.mark.parametrize(
    ("locale", "text"),
    [
        (Locale.ES_MX, "USD 1,250.50"),
        (Locale.ES_CO, "USD 1.250,50"),
        (Locale.ES_AR, "USD 1.250,50"),
        (Locale.PT_BR, "USD 1.250,50"),
    ],
)
def test_same_usd_amount_per_locale(locale: Locale, text: str) -> None:
    assert format_amount(Decimal("1250.5"), "USD", locale) == text


@pytest.mark.parametrize(
    ("amount", "currency", "locale", "text"),
    [
        (Decimal("1250000"), "COP", Locale.ES_CO, "COP 1.250.000,00"),
        (Decimal("40000000"), "ARS", Locale.ES_AR, "ARS 40.000.000,00"),
        (Decimal("0.005"), "usd", Locale.ES_MX, "USD 0.01"),
        (Decimal("18.9"), " brl ", Locale.PT_BR, "BRL 18,90"),
    ],
)
def test_format_amount(amount: Decimal, currency: str, locale: Locale, text: str) -> None:
    formatted = format_amount(amount, currency, locale)
    assert formatted == text
    assert isinstance(formatted, FormattedAmount)


@pytest.mark.parametrize("currency", ["", "$", "US", "DOLLAR", "12A"])
def test_format_amount_needs_a_currency_code(currency: str) -> None:
    with pytest.raises(ValueError, match="currency code"):
        format_amount(Decimal("1"), currency, Locale.ES_CO)


@pytest.mark.parametrize("locale", list(Locale))
@pytest.mark.parametrize("currency", ["USD", "COP", "ARS"])
def test_no_amount_is_shown_without_its_currency_code(locale: Locale, currency: str) -> None:
    text = format_amount(Decimal("987654.321"), currency, locale)
    assert re.fullmatch(rf"{currency} \d{{1,3}}(?:[.,]\d{{3}})*[.,]\d{{2}}", text)
    assert "$" not in text


@pytest.mark.parametrize("value", [50.0, Decimal("50"), "USD 50.00", "50.00 USD", 50])
def test_raw_amounts_are_never_rendered(templates: TemplateService, value: object) -> None:
    with pytest.raises(TemplateError, match="must come from format_amount"):
        templates.render("clarify_duplicate_ref", "es", transaction_date="16/06/2026", amount=value)


def test_every_amount_placeholder_is_checked(templates: TemplateService) -> None:
    with_amount = [t for t in templates.template_ids if "amount" in templates.placeholders(t)]
    assert set(with_amount) == {
        "confirm_summary",
        "candidate_line",
        "clarify_duplicate_ref",
        "clarify_duplicate_ref_again",
        "identified_transaction",
    }


def test_format_date() -> None:
    assert format_date(date(2026, 6, 5)) == "05/06/2026"
    assert format_date(datetime(2026, 6, 18, 3, 0)) == "18/06/2026"


# --- Labels ---------------------------------------------------------------------------


@pytest.mark.parametrize("language", ["es", "pt"])
def test_every_label_exists(templates: TemplateService, language: str) -> None:
    for code in ReasonCode:
        assert templates.label("reason_code", code.value, language)
    for status in CaseStatus:
        if status is CaseStatus.DRAFT:
            continue
        assert templates.label("case_status", status.value, language)
    for action in (ActionId.CREATE_CASE, ActionId.BLOCK_CARD):
        assert templates.label("action", action.value, language)


def test_unknown_label_fails(templates: TemplateService) -> None:
    with pytest.raises(TemplateError, match="unknown label"):
        templates.label("reason_code", "RC_OTHER", "es")


# --- Composition and specific cases ------------------------------------------------------


def test_tool_failure_never_closes_a_turn_alone(templates: TemplateService) -> None:
    follow_ups = REQUIRED_FOLLOW_UPS["tool_failure"]
    assert follow_ups == {"handoff", "offer_transfer"}
    assert follow_ups <= templates.template_ids


def test_every_side_question_has_an_answer_without_promises(templates: TemplateService) -> None:
    assert set(SIDE_TEMPLATES) == set(SideQuestion)
    assert REQUIRED_FOLLOW_UPS["no_match"] == {"ask_transaction_detail"}
    for template_id in SIDE_TEMPLATES.values():
        for language in ("es", "pt"):
            values = values_for(templates, template_id)
            assert not find_promises(templates.render(template_id, language, **values))


def test_refund_and_timeline_answers_take_the_days_from_the_policy(
    templates: TemplateService,
) -> None:
    days = load_policy_config().parameters.RESOLUTION_TARGET_BUSINESS_DAYS
    refund = templates.render("side_refund", "es")
    assert refund.startswith("No puedo confirmarle un reembolso.")
    assert f"hasta {days} días hábiles" in refund
    assert f"até {days} dias úteis" in templates.render("side_timeline", "pt")
    with pytest.raises(TemplateError, match="come from the policy"):
        templates.render("side_refund", "es", days=1)


def test_session_expired_mid_conversation_asks_to_reauthenticate_and_reconfirm(
    templates: TemplateService,
) -> None:
    es = templates.render("session_expired_reconfirm", "es")
    pt = templates.render("session_expired_reconfirm", "pt")
    assert "expiró" in es and "resumen" in es and "confirme" in es
    assert "expirou" in pt and "resumo" in pt and "confirmar" in pt


def test_attempts_exceeded_has_its_own_inform_text(templates: TemplateService) -> None:
    exceeded = templates.inform(InformReason.AUTHENTICATION_ATTEMPTS_EXCEEDED, "es")
    declined = templates.inform(InformReason.AUTHENTICATION_DECLINED, "es")
    assert "varios intentos" in exceeded
    assert exceeded != declined


def test_unauthenticated_handoff_mentions_no_account_data(templates: TemplateService) -> None:
    assert templates.placeholders("handoff_unauthenticated") == frozenset()
    for language in ("es", "pt"):
        text = templates.render("handoff_unauthenticated", language).lower()
        for word in (
            "tarjeta",
            "cartão",
            "cuenta",
            "conta",
            "transac",
            "monto",
            "valor",
            "caso",
            "información que",
            "informações que",
        ):
            assert word not in text, (language, word)


def test_each_confirmation_covers_exactly_one_action() -> None:
    assert CONFIRMATION_TEMPLATES == {
        "confirm_block_card": ActionId.BLOCK_CARD,
        "confirm_summary": ActionId.CREATE_CASE,
    }
    assert len(set(CONFIRMATION_TEMPLATES.values())) == len(CONFIRMATION_TEMPLATES)
    # The card block is confirmed first, then the dispute summary.
    assert CONFIRMATION_ORDER == ("confirm_block_card", "confirm_summary")


@pytest.mark.parametrize("language", ["es", "pt"])
def test_confirm_summary_never_lists_the_card_block(
    templates: TemplateService, language: str
) -> None:
    assert "action" not in templates.placeholders("confirm_summary")
    text = templates.render("confirm_summary", language, **values_for(templates, "confirm_summary"))
    assert templates.label("action", ActionId.BLOCK_CARD.value, language) not in text
    assert not re.search(r"bloque|bloqueio", text.lower())


def test_confirm_block_card_never_lists_the_dispute(templates: TemplateService) -> None:
    for language in ("es", "pt"):
        text = templates.render("confirm_block_card", language, product="****4821")
        assert templates.label("action", ActionId.CREATE_CASE.value, language) not in text


def test_card_block_confirmation_is_informed(templates: TemplateService) -> None:
    es = templates.render("confirm_block_card", "es", product="****4821")
    pt = templates.render("confirm_block_card", "pt", product="****4821")
    assert "incluidos los pagos automáticos" in es
    assert "Si más adelante necesita desbloquearla, puede solicitarlo a un agente" in es
    assert "incluindo os pagamentos automáticos" in pt
    assert "precisar desbloqueá-lo, pode pedir isso a um atendente" in pt
    label_es = templates.label("action", "ACT-03", "es")
    label_pt = templates.label("action", "ACT-03", "pt")
    assert "automáticos" in label_es and "automáticos" in label_pt
    for text in (es, pt, label_es, label_pt):
        assert not re.search(r"reposici|reposi[cç][aã]o|nueva tarjeta|novo cartão", text.lower())


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


@pytest.mark.parametrize(
    ("es", "pt", "message"),
    [
        (
            "Confirme: {transaction_date} {merchant} {amount} {product} {reason} {action}",
            "Confirme: {transaction_date} {merchant} {amount} {product} {reason} {action}",
            r"no \{action\}",
        ),
        (
            "Confirme {transaction_date} {merchant} {amount} {product} {reason}. "
            "Acción: Bloquear temporalmente la tarjeta (dejará de funcionar para todas las "
            "compras y pagos, incluidos los automáticos)",
            "Confirme {transaction_date} {merchant} {amount} {product} {reason}.",
            "lists ACT-03",
        ),
    ],
)
def test_confirm_summary_with_a_second_action_fails_at_load(
    tmp_path: Path, es: str, pt: str, message: str
) -> None:
    path = _with_template(tmp_path, "confirm_summary", {"es": es, "pt": pt})
    with pytest.raises(TemplateError, match=message):
        TemplateService(10, path)


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


@pytest.mark.parametrize("language", ["es", "pt"])
def test_ask_rephrase_reveals_nothing_about_the_detection(
    templates: TemplateService, language: str
) -> None:
    text = templates.render("ask_rephrase", language).lower()
    for word in (
        "detect",
        "instruc",
        "segur",
        "sospech",
        "suspeit",
        "manipul",
        "ataque",
        "bloque",
        "regla",
        "regra",
        "polític",
        "propias palabras",
        "próprias palavras",
    ):
        assert word not in text, word


@pytest.mark.parametrize("language", ["es", "pt"])
def test_handoff_after_esc_13_reveals_nothing_about_the_detection(
    templates: TemplateService, language: str
) -> None:
    # ESC-13 (strike 2) sends the generic handoff: it must not hint at the detection either.
    for template_id in ("handoff", "handoff_unauthenticated"):
        text = templates.render(template_id, language).lower()
        for word in (
            "detect",
            "sospech",
            "suspeit",
            "manipul",
            "ataque",
            "intento",
            "tentativa",
            "instruc",
            "fraude",
            "seguridad",
            "segurança",
            "security",
        ):
            assert word not in text, (template_id, word)


def test_draft_status_has_no_customer_label(templates: TemplateService) -> None:
    # Draft exists only inside a conversation; GATE-11 does not count it (policy §5).
    for language in ("es", "pt"):
        with pytest.raises(TemplateError, match="unknown label"):
            templates.label("case_status", CaseStatus.DRAFT.value, language)


@pytest.mark.parametrize("language", ["es", "pt"])
def test_duplicate_case_never_shows_a_draft(templates: TemplateService, language: str) -> None:
    for status in CaseStatus:
        if status is CaseStatus.DRAFT:
            continue
        text = templates.render(
            "duplicate_case",
            language,
            case_ref="DSP-1",
            status=templates.label("case_status", status.value, language),
        ).lower()
        assert "borrador" not in text
        assert "rascunho" not in text


def test_portuguese_uses_estabelecimento_for_merchant() -> None:
    for entry in RAW["templates"].values():
        assert "loja" not in entry["pt"].lower()


def test_every_clarify_target_has_a_template(templates: TemplateService) -> None:
    from app.contracts import ClarifyTarget
    from app.templates.service import CLARIFY_TARGET_TEMPLATES

    assert set(CLARIFY_TARGET_TEMPLATES) == set(ClarifyTarget)
    assert set(CLARIFY_TARGET_TEMPLATES.values()) <= templates.template_ids


def test_withdrawn_and_correction_texts(templates: TemplateService) -> None:
    assert "no registré ninguna disputa" in templates.render("dispute_withdrawn", "es")
    assert "não registrei nenhuma contestação" in templates.render("dispute_withdrawn", "pt")
    assert "¿Qué dato no es correcto?" in templates.render("ask_correction", "es")
    assert "Qual dado não está correto?" in templates.render("ask_correction", "pt")


def test_no_template_shows_a_policy_value_other_than_the_resolution_days(
    templates: TemplateService,
) -> None:
    # COM-07: windows and thresholds stay internal. {days} (RESOLUTION_TARGET_BUSINESS_DAYS) is
    # the only policy value a customer sees, and no template writes a number of its own.
    assert POLICY_VALUES == {"days"}
    parameters = {name.lower() for name in type(load_policy_config().parameters).model_fields}
    for template_id in templates.template_ids:
        assert not templates.placeholders(template_id) & (parameters | {"window_days"})
    for text in _all_texts():
        assert not re.search(r"\d", text), text


def test_no_rendered_template_shows_an_amount_without_its_currency(
    templates: TemplateService,
) -> None:
    uncoded = re.compile(r"(?<![A-Z]{3} )(?<![\d.,])\d{1,3}(?:[.,]\d{3})*[.,]\d{2}(?!\d)")
    for template_id in templates.template_ids:
        for language in ("es", "pt"):
            text = templates.render(template_id, language, **values_for(templates, template_id))
            assert not uncoded.search(text), (template_id, text)


@pytest.mark.parametrize(
    ("message", "country", "currency"),
    [
        ("fue como de 40 dólares", None, "USD"),
        ("uns R$ 40", None, "BRL"),
        ("unos 40 reales", None, "BRL"),
        ("40 USD", None, "USD"),
        ("unos 160000 pesos", "Colombia", "COP"),
        ("unos 900 pesos", "México", "MXN"),
        ("unos 900 pesos", None, None),  # whose pesos? none named
        ("$40", None, None),  # "$" alone is ambiguous
        ("unos 40", None, None),
        ("40 dólares o 160000 pesos", "Colombia", None),  # two currencies: none
    ],
)
def test_the_currency_the_customer_named(
    message: str, country: str | None, currency: str | None
) -> None:
    assert currency_said(message, country) == currency


def test_a_plain_number_is_never_formatted_like_an_amount() -> None:
    assert plain_number(Decimal("40"), Locale.ES_CO) == "40"
    assert plain_number(Decimal("18.90"), Locale.ES_CO) == "18,9"
    assert plain_number(Decimal("1250.5"), Locale.ES_MX) == "1250.5"
