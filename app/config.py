"""Policy configuration: loads and validates ``config/policy.yaml``.

Parameter names match docs/dispute-policy.md §15 exactly, so a reader can grep the policy
and the code with the same identifier. Validation is strict: a missing parameter, an unknown
parameter, or a value of the wrong type stops the application at startup.
"""

from __future__ import annotations

import os
import unicodedata
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self

import yaml
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictInt,
    ValidationError,
    model_validator,
)

DEFAULT_POLICY_PATH = Path(__file__).resolve().parent.parent / "config" / "policy.yaml"
DEFAULT_MERCHANT_CATEGORIES_PATH = DEFAULT_POLICY_PATH.parent / "merchant_categories.yaml"
POLICY_PATH_ENV = "POLICY_CONFIG_PATH"


class PolicyConfigError(RuntimeError):
    """Raised when the policy configuration file is missing or invalid."""


def _to_decimal(value: Any) -> Any:
    # YAML yields int or float; strings and booleans are rejected so a typo cannot pass.
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise ValueError("must be a number")
    return Decimal(str(value))


def _strict_probability(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ValueError("must be a number between 0 and 1, or null")
    return float(value)


PositiveInt = Annotated[StrictInt, Field(gt=0)]
NonNegativeInt = Annotated[StrictInt, Field(ge=0)]
UsdAmount = Annotated[Decimal, BeforeValidator(_to_decimal), Field(gt=0)]
OptionalProbability = Annotated[
    Annotated[float, Field(ge=0.0, le=1.0)] | None, BeforeValidator(_strict_probability)
]


class PolicyParameters(BaseModel):
    """The parameters of docs/dispute-policy.md §15."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    SESSION_MAX_AGE_MIN: PositiveInt
    SESSION_IDLE_TIMEOUT_MIN: PositiveInt
    AUTH_MAX_ATTEMPTS: PositiveInt
    DISPUTE_WINDOW_DAYS: PositiveInt
    LATE_WINDOW_DAYS: PositiveInt
    DUPLICATE_WINDOW_HOURS: PositiveInt
    FX_MAX_STALENESS_DAYS: NonNegativeInt
    PROVISIONAL_CREDIT_AUTO_MAX_USD: UsdAmount
    AUTO_INTAKE_MAX_USD: UsdAmount
    AGG_DISPUTED_30D_MAX_USD: UsdAmount
    REPEAT_DISPUTES_90D: PositiveInt
    UNRECOGNIZED_BATCH_MAX: PositiveInt
    FRAUD_SCORE_ESCALATE: Annotated[StrictInt, Field(ge=0, le=100)]
    MAX_CLARIFICATION_TURNS: PositiveInt
    MAX_TOTAL_CLARIFICATIONS: PositiveInt
    MAX_CANDIDATES_SHOWN: PositiveInt
    AMOUNT_TOLERANCE_PCT: Annotated[StrictInt, Field(ge=0, le=100)]
    AMOUNT_TOLERANCE_USD: UsdAmount
    TOOL_MAX_RETRIES: NonNegativeInt
    INJECTION_STRIKES_MAX: PositiveInt
    # Calibrated on the validation split; null until calibration (policy §15, §16.4).
    DECISION_CONFIDENCE_MIN: OptionalProbability
    ESCALATION_RISK_THRESHOLD: OptionalProbability
    RESOLUTION_TARGET_BUSINESS_DAYS: PositiveInt

    @model_validator(mode="after")
    def _check_consistency(self) -> Self:
        # Orderings the policy's definitions depend on; any other state makes a rule unreachable.
        if self.SESSION_IDLE_TIMEOUT_MIN > self.SESSION_MAX_AGE_MIN:
            raise ValueError("SESSION_IDLE_TIMEOUT_MIN must not exceed SESSION_MAX_AGE_MIN")
        if self.DISPUTE_WINDOW_DAYS >= self.LATE_WINDOW_DAYS:
            raise ValueError("DISPUTE_WINDOW_DAYS must be lower than LATE_WINDOW_DAYS")
        if self.PROVISIONAL_CREDIT_AUTO_MAX_USD >= self.AUTO_INTAKE_MAX_USD:
            raise ValueError(
                "PROVISIONAL_CREDIT_AUTO_MAX_USD must be lower than AUTO_INTAKE_MAX_USD"
            )
        return self


class PolicyConfig(BaseModel):
    """Versioned policy configuration. ``policy_version`` matches the policy document."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+$")]
    parameters: PolicyParameters


def load_policy_config(path: Path | str | None = None) -> PolicyConfig:
    """Load and validate the policy file. Raises ``PolicyConfigError`` with a clear message."""
    resolved = Path(path) if path is not None else _default_path()
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PolicyConfigError(f"Policy file not found: {resolved}") from exc
    except yaml.YAMLError as exc:
        raise PolicyConfigError(f"Policy file is not valid YAML: {resolved}: {exc}") from exc

    if not isinstance(raw, dict):
        raise PolicyConfigError(f"Policy file must contain a mapping: {resolved}")

    try:
        return PolicyConfig.model_validate(raw)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        )
        raise PolicyConfigError(f"Invalid policy file {resolved}: {problems}") from exc


def load_merchant_categories(path: Path | str | None = None) -> dict[str, str]:
    """Word -> merchant category for GATE-05 (config/merchant_categories.yaml). Words are
    folded (lower case, no accents); a word in two categories is an error."""
    resolved = Path(path) if path is not None else DEFAULT_MERCHANT_CATEGORIES_PATH
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except (FileNotFoundError, yaml.YAMLError) as exc:
        raise PolicyConfigError(f"Merchant categories not readable: {resolved}: {exc}") from exc
    categories = raw.get("categories") if isinstance(raw, dict) else None
    if not isinstance(categories, dict) or not categories:
        raise PolicyConfigError(f"Merchant categories need a 'categories' mapping: {resolved}")
    words: dict[str, str] = {}
    for category, entries in categories.items():
        if not isinstance(entries, list) or not entries:
            raise PolicyConfigError(f"Merchant category {category} needs a list of words")
        for entry in entries:
            decomposed = unicodedata.normalize("NFKD", str(entry).strip().casefold())
            word = "".join(char for char in decomposed if not unicodedata.combining(char))
            if not word.isalnum():
                raise PolicyConfigError(f"Merchant category word must be one word: {entry!r}")
            if word in words and words[word] != category:
                raise PolicyConfigError(f"{word!r} is in {words[word]} and {category}")
            words[word] = str(category)
    return words


def _default_path() -> Path:
    override = os.environ.get(POLICY_PATH_ENV)
    return Path(override) if override else DEFAULT_POLICY_PATH


@lru_cache(maxsize=1)
def get_policy_config() -> PolicyConfig:
    """Process-wide policy configuration, loaded once."""
    return load_policy_config()
