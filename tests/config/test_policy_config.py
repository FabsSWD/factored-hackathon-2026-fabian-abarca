from __future__ import annotations

import re
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import (
    DEFAULT_POLICY_PATH,
    POLICY_PATH_ENV,
    PolicyConfig,
    PolicyConfigError,
    get_policy_config,
    load_policy_config,
)

# docs/dispute-policy.md §15, value by value. If this table changes, the policy changed.
POLICY_TABLE: dict[str, int | Decimal | None] = {
    "SESSION_MAX_AGE_MIN": 60,
    "SESSION_IDLE_TIMEOUT_MIN": 15,
    "DISPUTE_WINDOW_DAYS": 60,
    "LATE_WINDOW_DAYS": 120,
    "DUPLICATE_WINDOW_HOURS": 48,
    "PROVISIONAL_CREDIT_AUTO_MAX_USD": Decimal("100"),
    "AUTO_INTAKE_MAX_USD": Decimal("1000"),
    "AGG_DISPUTED_30D_MAX_USD": Decimal("2000"),
    "REPEAT_DISPUTES_90D": 3,
    "UNRECOGNIZED_BATCH_MAX": 3,
    "FRAUD_SCORE_ESCALATE": 80,
    "MAX_CLARIFICATION_TURNS": 2,
    "MAX_TOTAL_CLARIFICATIONS": 4,
    "MAX_CANDIDATES_SHOWN": 3,
    "TOOL_MAX_RETRIES": 2,
    "INJECTION_STRIKES_MAX": 2,
    "DECISION_CONFIDENCE_MIN": None,
    "ESCALATION_RISK_THRESHOLD": None,
    "RESOLUTION_TARGET_BUSINESS_DAYS": 10,
}


def _raw_policy() -> dict[str, Any]:
    loaded = yaml.safe_load(DEFAULT_POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write(tmp_path: Path, data: object) -> Path:
    path = tmp_path / "policy.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _with_parameter(tmp_path: Path, name: str, value: object) -> Path:
    raw = _raw_policy()
    raw["parameters"][name] = value
    return _write(tmp_path, raw)


# --- Valid load -------------------------------------------------------------


def test_repository_policy_file_loads() -> None:
    config = load_policy_config(DEFAULT_POLICY_PATH)
    assert isinstance(config, PolicyConfig)
    assert config.policy_version == "0.2.0"


def test_policy_version_matches_the_policy_document() -> None:
    document = DEFAULT_POLICY_PATH.parent.parent / "docs" / "dispute-policy.md"
    match = re.search(r"^\| Version \| (\S+) \|$", document.read_text(encoding="utf-8"), re.M)
    assert match is not None
    assert load_policy_config(DEFAULT_POLICY_PATH).policy_version == match.group(1)


def test_policy_has_exactly_the_nineteen_parameters_of_section_15() -> None:
    assert len(POLICY_TABLE) == 19
    assert set(_raw_policy()["parameters"]) == set(POLICY_TABLE)


@pytest.mark.parametrize(("name", "expected"), sorted(POLICY_TABLE.items()))
def test_each_value_matches_the_policy_table(name: str, expected: int | Decimal | None) -> None:
    config = load_policy_config(DEFAULT_POLICY_PATH)
    actual = getattr(config.parameters, name)
    assert actual == expected
    assert type(actual) is type(expected)


def test_money_parameters_are_decimals() -> None:
    params = load_policy_config(DEFAULT_POLICY_PATH).parameters
    assert isinstance(params.AUTO_INTAKE_MAX_USD, Decimal)


def test_calibrated_probabilities_accept_values_in_range(tmp_path: Path) -> None:
    raw = _raw_policy()
    raw["parameters"]["DECISION_CONFIDENCE_MIN"] = 0.7
    raw["parameters"]["ESCALATION_RISK_THRESHOLD"] = 1
    config = load_policy_config(_write(tmp_path, raw))
    assert config.parameters.DECISION_CONFIDENCE_MIN == 0.7
    assert config.parameters.ESCALATION_RISK_THRESHOLD == 1.0


def test_float_money_value_is_accepted_exactly(tmp_path: Path) -> None:
    config = load_policy_config(_with_parameter(tmp_path, "AUTO_INTAKE_MAX_USD", 1000.5))
    assert config.parameters.AUTO_INTAKE_MAX_USD == Decimal("1000.5")


def test_config_is_immutable() -> None:
    config = load_policy_config(DEFAULT_POLICY_PATH)
    with pytest.raises(ValueError, match="frozen"):
        config.parameters.AUTO_INTAKE_MAX_USD = Decimal("5")  # type: ignore[misc]


def test_env_variable_selects_the_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    raw = _raw_policy()
    raw["policy_version"] = "9.9.9"
    monkeypatch.setenv(POLICY_PATH_ENV, str(_write(tmp_path, raw)))
    assert load_policy_config().policy_version == "9.9.9"


def test_default_path_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_PATH_ENV, raising=False)
    assert load_policy_config().policy_version == "0.2.0"


def test_get_policy_config_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(POLICY_PATH_ENV, raising=False)
    get_policy_config.cache_clear()
    try:
        assert get_policy_config() is get_policy_config()
    finally:
        get_policy_config.cache_clear()


# --- Clear failures ---------------------------------------------------------


@pytest.mark.parametrize("name", sorted(POLICY_TABLE))
def test_missing_parameter_fails_naming_it(tmp_path: Path, name: str) -> None:
    raw = _raw_policy()
    del raw["parameters"][name]
    with pytest.raises(PolicyConfigError, match=rf"parameters\.{name}: Field required"):
        load_policy_config(_write(tmp_path, raw))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SESSION_MAX_AGE_MIN", "60"),
        ("SESSION_MAX_AGE_MIN", 60.5),
        ("SESSION_MAX_AGE_MIN", True),
        ("REPEAT_DISPUTES_90D", None),
        ("AUTO_INTAKE_MAX_USD", "1000"),
        ("AUTO_INTAKE_MAX_USD", True),
        ("AUTO_INTAKE_MAX_USD", None),
        ("DECISION_CONFIDENCE_MIN", "0.5"),
        ("DECISION_CONFIDENCE_MIN", False),
        ("FRAUD_SCORE_ESCALATE", [80]),
    ],
)
def test_wrong_type_fails_naming_the_parameter(tmp_path: Path, name: str, value: object) -> None:
    with pytest.raises(PolicyConfigError, match=rf"parameters\.{name}"):
        load_policy_config(_with_parameter(tmp_path, name, value))


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SESSION_MAX_AGE_MIN", 0),
        ("DISPUTE_WINDOW_DAYS", -1),
        ("AUTO_INTAKE_MAX_USD", 0),
        ("TOOL_MAX_RETRIES", -1),
        ("FRAUD_SCORE_ESCALATE", 101),
        ("DECISION_CONFIDENCE_MIN", 1.5),
        ("ESCALATION_RISK_THRESHOLD", -0.1),
    ],
)
def test_out_of_range_value_fails(tmp_path: Path, name: str, value: object) -> None:
    with pytest.raises(PolicyConfigError, match=rf"parameters\.{name}"):
        load_policy_config(_with_parameter(tmp_path, name, value))


def test_zero_retries_is_allowed(tmp_path: Path) -> None:
    config = load_policy_config(_with_parameter(tmp_path, "TOOL_MAX_RETRIES", 0))
    assert config.parameters.TOOL_MAX_RETRIES == 0


def test_unknown_parameter_fails(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match=r"parameters\.MYSTERY_LIMIT"):
        load_policy_config(_with_parameter(tmp_path, "MYSTERY_LIMIT", 1))


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"SESSION_IDLE_TIMEOUT_MIN": 61}, "SESSION_IDLE_TIMEOUT_MIN"),
        ({"DISPUTE_WINDOW_DAYS": 120}, "DISPUTE_WINDOW_DAYS"),
        ({"PROVISIONAL_CREDIT_AUTO_MAX_USD": 1000}, "PROVISIONAL_CREDIT_AUTO_MAX_USD"),
    ],
)
def test_inconsistent_orderings_fail(tmp_path: Path, changes: dict[str, int], message: str) -> None:
    raw = _raw_policy()
    raw["parameters"].update(changes)
    with pytest.raises(PolicyConfigError, match=message):
        load_policy_config(_write(tmp_path, raw))


@pytest.mark.parametrize("version", ["", "0.1", "v0.1.0", None])
def test_invalid_policy_version_fails(tmp_path: Path, version: object) -> None:
    raw = _raw_policy()
    raw["policy_version"] = version
    with pytest.raises(PolicyConfigError, match="policy_version"):
        load_policy_config(_write(tmp_path, raw))


def test_missing_file_fails(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match="not found"):
        load_policy_config(tmp_path / "absent.yaml")


def test_invalid_yaml_fails(tmp_path: Path) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text("parameters: [unclosed", encoding="utf-8")
    with pytest.raises(PolicyConfigError, match="not valid YAML"):
        load_policy_config(path)


@pytest.mark.parametrize("content", ["", "- a\n- b\n", "just text"])
def test_non_mapping_file_fails(tmp_path: Path, content: str) -> None:
    path = tmp_path / "policy.yaml"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(PolicyConfigError, match="must contain a mapping"):
        load_policy_config(path)


def test_missing_parameters_section_fails(tmp_path: Path) -> None:
    with pytest.raises(PolicyConfigError, match=r"parameters: Field required"):
        load_policy_config(_write(tmp_path, {"policy_version": "0.1.0"}))
