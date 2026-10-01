from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from app.settings import Settings, business_as_of, get_settings


def test_as_of_is_the_end_of_the_business_day() -> None:
    settings = Settings(_env_file=None, business_date=date(2026, 6, 17))
    assert settings.business_day_cutoff == time(6, 0)
    assert settings.as_of == datetime(2026, 6, 18, 6, 0)
    assert settings.as_of.tzinfo is None


def test_cutoff_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUSINESS_DATE", "2026-06-17")
    monkeypatch.setenv("BUSINESS_DAY_CUTOFF", "00:00")
    settings = Settings(_env_file=None)
    assert settings.as_of == datetime(2026, 6, 18, 0, 0)


def test_business_as_of_crosses_month_end() -> None:
    assert business_as_of(date(2026, 6, 30), time(6, 0)) == datetime(2026, 7, 1, 6, 0)


def test_business_date_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("BUSINESS_DATE", raising=False)
    with pytest.raises(ValidationError, match="business_date"):
        Settings(_env_file=None)


@pytest.mark.parametrize("value", ["17/06/2026", "tomorrow"])
def test_invalid_business_date_is_rejected(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("BUSINESS_DATE", value)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_reads_env_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env = tmp_path / ".env"
    env.write_text("BUSINESS_DATE=2026-01-31\nDOCUMENT_HASH_KEY=abc\n", encoding="utf-8")
    monkeypatch.delenv("BUSINESS_DATE", raising=False)
    settings = Settings(_env_file=env)
    assert settings.business_date == date(2026, 1, 31)
    assert settings.document_hash_key is not None
    assert settings.document_hash_key.get_secret_value() == "abc"
    assert "abc" not in repr(settings)


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BUSINESS_DATE", "2026-06-17")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_owner_url_prefers_the_migration_url() -> None:
    base: dict[str, Any] = {"_env_file": None, "business_date": date(2026, 6, 17)}
    both = Settings(
        **base, database_url="postgresql://app", migration_database_url="postgresql://owner"
    )
    assert both.owner_database_url == "postgresql://owner"
    single = Settings(**base, database_url="postgresql://app")
    assert single.owner_database_url == "postgresql://app"
