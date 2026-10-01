"""Runtime settings read from environment variables (and ``.env`` in development).

Policy parameters live in ``config/policy.yaml`` (see ``app.config``); this module holds
deployment settings only.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.audit.masking import AuditMessageMode

AGENT_TOKEN_MIN_LENGTH = 32


class PseudonymKeyMissingError(RuntimeError):
    """PSEUDONYM_KEY is required to start the application."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", frozen=True)

    # The application role: SELECT on Core Banking, no DDL (scripts/sql/roles.sql).
    database_url: str | None = None
    # The owner role: migrations and the Core Banking load. Falls back to DATABASE_URL for a
    # single-role local setup.
    migration_database_url: str | None = None
    test_database_url: str | None = None

    # Business clock (dispute policy §15, "Business date"). Dataset timestamps are naive local
    # times, so ``as_of`` is naive too.
    business_date: date
    business_day_cutoff: time = time(6, 0)

    document_hash_key: SecretStr | None = None
    # Key of the pseudonymous customer references (app/pseudonym.py), separate from
    # DOCUMENT_HASH_KEY so each key can be rotated on its own. Required at startup.
    pseudonym_key: SecretStr | None = None

    # Identity service (mock of an identity provider). Session age and idle limits are policy
    # parameters (GATE-02); these settings belong to the provider itself.
    jwt_secret: SecretStr | None = None
    test_otp: SecretStr | None = None
    otp_ttl_min: int = Field(default=5, gt=0)
    otp_max_failures: int = Field(default=3, gt=0)
    otp_failure_window_min: int = Field(default=15, gt=0)

    # Language model (OpenAI). The architecture fixes GPT-6 Luna; the exact model name comes
    # from LLM_MODEL.
    openai_api_key: SecretStr | None = None
    llm_model: str | None = None
    openai_base_url: str = "https://api.openai.com/v1"
    llm_timeout_seconds: float = Field(default=20.0, gt=0)
    llm_max_retries: int = Field(default=2, ge=0)
    # Total budget for the model calls of one turn; no retry starts if it does not fit.
    llm_turn_deadline_seconds: float = Field(default=20.0, gt=0)
    # Connecting sentences around templates; off to compare latency and cost (M18).
    llm_connect_enabled: bool = True
    # LLM tokens per conversation (architecture §7); past it the LLM is not called again.
    llm_max_tokens_per_conversation: int | None = Field(default=None, gt=0)

    # Decision model (Kev, TypeSafe API). Empty base URL: Kev is not configured.
    kev_base_url: str | None = None
    kev_timeout_seconds: float = Field(default=2.0, gt=0)

    # Audit (M11): the agent role's bearer token, what the trace keeps of the customer's
    # message, and the token rates for the estimated cost (unknown without them).
    agent_api_token: SecretStr | None = None
    audit_message_mode: AuditMessageMode = AuditMessageMode.MASKED
    llm_input_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    llm_output_usd_per_mtok: Decimal | None = Field(default=None, ge=0)
    llm_cached_input_usd_per_mtok: Decimal | None = Field(default=None, ge=0)

    @field_validator(
        "llm_input_usd_per_mtok",
        "llm_output_usd_per_mtok",
        "llm_cached_input_usd_per_mtok",
        "llm_max_tokens_per_conversation",
        mode="before",
    )
    @classmethod
    def _empty_rate_is_unknown(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    # Abuse limits: per session on authenticated routes, per client IP on /auth/login and
    # /auth/verify (which have no session yet).
    rate_limit_requests_per_minute: int = Field(default=20, gt=0)
    auth_rate_limit_per_minute: int = Field(default=10, gt=0)

    def require_pseudonym_key(self) -> str:
        """The pseudonym key, or ``PseudonymKeyMissingError`` (the application does not start
        without it)."""
        key = self.pseudonym_key.get_secret_value() if self.pseudonym_key else ""
        if not key:
            raise PseudonymKeyMissingError(
                "PSEUDONYM_KEY is not set: it keys the pseudonymous customer references"
            )
        return key

    def agent_token(self) -> str | None:
        """The agent role's token, or None (audit API unavailable). Too short a token is an
        error at startup."""
        token = self.agent_api_token.get_secret_value() if self.agent_api_token else ""
        if not token:
            return None
        if len(token) < AGENT_TOKEN_MIN_LENGTH:
            raise ValueError(
                f"AGENT_API_TOKEN must have at least {AGENT_TOKEN_MIN_LENGTH} characters"
            )
        return token

    @property
    def owner_database_url(self) -> str | None:
        return self.migration_database_url or self.database_url

    @property
    def as_of(self) -> datetime:
        """End of the business day: BUSINESS_DATE + 1 day at BUSINESS_DAY_CUTOFF."""
        return business_as_of(self.business_date, self.business_day_cutoff)


def business_as_of(business_date: date, cutoff: time) -> datetime:
    return datetime.combine(business_date + timedelta(days=1), cutoff)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # values come from the environment
