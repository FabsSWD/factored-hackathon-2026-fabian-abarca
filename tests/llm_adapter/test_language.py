from __future__ import annotations

import pytest
import yaml

from app.contracts import Language
from app.llm_adapter import prompts
from app.llm_adapter.language import guess_language
from app.templates.service import DEFAULT_TEMPLATES_PATH

TEMPLATES = yaml.safe_load(DEFAULT_TEMPLATES_PATH.read_text(encoding="utf-8"))["templates"]


@pytest.mark.parametrize("template_id", sorted(TEMPLATES))
def test_every_template_is_never_taken_for_the_other_language(template_id: str) -> None:
    for code in ("es", "pt"):
        guess = guess_language(TEMPLATES[template_id][code])
        assert guess in (Language(code), None), (template_id, code, guess)


def test_only_bilingual_or_wordless_templates_give_no_signal() -> None:
    silent = {
        template_id
        for template_id, entry in TEMPLATES.items()
        for code in ("es", "pt")
        if guess_language(entry[code]) is None
    }
    assert silent == {"ask_language", "candidate_line"}


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("Entiendo su preocupación.", Language.ES),
        ("Gracias por su paciencia.", Language.ES),
        ("Lamento lo ocurrido.", Language.ES),
        ("Entendo sua preocupação.", Language.PT),
        ("Obrigado pela paciência.", Language.PT),
        ("Lamento o ocorrido.", Language.PT),
        ("Claro.", None),
        ("", None),
    ],
)
def test_short_sentences(text: str, language: Language | None) -> None:
    assert guess_language(text) is language


def test_extract_prompt_rules() -> None:
    assert prompts.EXTRACT_PROMPT_VERSION == "extract@1.6.0"
    text = " ".join(prompts.EXTRACT_SYSTEM.split())
    assert "Always fill transaction_date, amount" in text
    assert "transaction_date is {day, month, year}" in text
    assert "never guess the year; the code completes it" in text
    assert "expected_delivery_date: the date the delivery was due, as {day, month, year}" in text
    assert "resolve it against context.business_date and give the full date" in text
    assert "whether or not the context lists candidates" in text
    assert "Fill transaction_id only with the alias of a transaction" in text
    assert "context.shown_candidates" in text
    assert "identified only by an alias (C1, C2, ...)" in text
    assert "always in the third person and in the conversation's language" in text
    assert '"El cliente indica que..."' in text
    assert '"O cliente informa que..."' in text
