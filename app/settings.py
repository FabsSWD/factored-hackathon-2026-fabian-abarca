"""Runtime settings read from environment variables (and ``.env`` in development).

Policy parameters live in ``config/policy.yaml`` (see ``app.config``); this module holds
deployment settings only.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    database_url: str | None = None
    test_database_url: str | None = None

    # Business clock (dispute policy §15, "Business date"). Dataset timestamps are naive local
    # times, so ``as_of`` is naive too.
    business_date: date
    business_day_cutoff: time = time(6, 0)

    document_hash_key: SecretStr | None = None

    @property
    def as_of(self) -> datetime:
        """End of the business day: BUSINESS_DATE + 1 day at BUSINESS_DAY_CUTOFF."""
        return business_as_of(self.business_date, self.business_day_cutoff)


def business_as_of(business_date: date, cutoff: time) -> datetime:
    return datetime.combine(business_date + timedelta(days=1), cutoff)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
