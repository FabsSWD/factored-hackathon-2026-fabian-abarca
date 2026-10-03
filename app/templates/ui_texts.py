"""Texts of the customer chat's interface (M14), loaded from config/ui_texts.yaml.

The frontend writes no text of its own: these labels, buttons, notices and errors reach it
through ``GET /api/ui/texts/{language}``, so Spanish and Portuguese are reviewed in one place.
They are interface texts, never commitments (case references, deadlines and outcomes come from
the templates, COM-02).
"""

from __future__ import annotations

import string
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

DEFAULT_UI_TEXTS_PATH = Path(__file__).resolve().parents[2] / "config" / "ui_texts.yaml"
LANGUAGES = ("es", "pt")


class UiTextsError(ValueError):
    """config/ui_texts.yaml is not valid."""


class UiTexts(BaseModel):
    """One language's catalog, as the frontend receives it."""

    model_config = ConfigDict(frozen=True)

    version: str
    language: str
    texts: dict[str, str]


def _placeholders(text: str) -> set[str]:
    return {name for _, name, _, _ in string.Formatter().parse(text) if name}


def load_ui_texts(path: Path | str | None = None) -> dict[str, UiTexts]:
    """Both languages; every text has ``es`` and ``pt``, non-empty, same placeholders."""
    source = Path(path) if path is not None else DEFAULT_UI_TEXTS_PATH
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (FileNotFoundError, yaml.YAMLError) as exc:
        raise UiTextsError(f"interface texts not readable: {source}: {exc}") from exc
    if not isinstance(raw, dict):
        raise UiTextsError("ui_texts.yaml must be a mapping")
    version, texts = raw.get("ui_texts_version"), raw.get("texts")
    if not isinstance(version, str) or version.count(".") != 2:
        raise UiTextsError("ui_texts_version must be a string like 1.0.0")
    if not isinstance(texts, dict) or not texts:
        raise UiTextsError("ui_texts.yaml needs a 'texts' mapping")
    by_language: dict[str, dict[str, str]] = {language: {} for language in LANGUAGES}
    for key, entry in texts.items():
        if not isinstance(entry, dict) or set(entry) != set(LANGUAGES):
            raise UiTextsError(f"{key}: needs exactly 'es' and 'pt'")
        values = {language: str(entry[language]).strip() for language in LANGUAGES}
        if not all(values.values()):
            raise UiTextsError(f"{key}: empty text")
        if _placeholders(values["es"]) != _placeholders(values["pt"]):
            raise UiTextsError(f"{key}: 'es' and 'pt' have different placeholders")
        for language in LANGUAGES:
            by_language[language][str(key)] = values[language]
    return {
        language: UiTexts(version=version, language=language, texts=by_language[language])
        for language in LANGUAGES
    }


@lru_cache(maxsize=1)
def ui_texts() -> dict[str, UiTexts]:
    return load_ui_texts()
