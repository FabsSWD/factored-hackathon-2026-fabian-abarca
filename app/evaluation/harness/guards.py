"""What must hold before the harness runs (policy §16.4).

- The evaluation split is the one fixed in M17: recomputed from the repository it equals
  ``split.yaml``, and its fingerprint is the pinned one.
- No evaluation case was used to calibrate: ``calibration_log.yaml`` records the cases behind
  every Calibrated parameter, none of them may be an evaluation case, and a Calibrated
  parameter set in ``config/policy.yaml`` must have an entry.
- The real cases are the locked selection: the local identifiers match the lock's hash.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict

from app.config import PolicyConfig
from app.evaluation.labeler import label_all
from app.evaluation.real import SelectionLock, load_lock, load_real_scenarios, selection_digest
from app.evaluation.scenarios import DEFAULT_SCENARIOS_DIR, load_scenarios
from app.evaluation.split import Split, make_split

EVALUATION_SPLIT_FINGERPRINT = "87e49ba9c5af29eeb1f8a8e8dba1df2991493e8c7124b97094f75a63af96cdd4"
CALIBRATION_LOG = DEFAULT_SCENARIOS_DIR / "calibration_log.yaml"
CALIBRATED_PARAMETERS = ("DECISION_CONFIDENCE_MIN", "ESCALATION_RISK_THRESHOLD")


class HarnessGuardError(RuntimeError):
    """The harness refuses to run."""


class CalibrationEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parameter: str
    value: float | None = None
    date: str | None = None
    cases: list[str]


def stored_split(path: Path | None = None) -> Split:
    raw = yaml.safe_load((path or DEFAULT_SCENARIOS_DIR / "split.yaml").read_text(encoding="utf-8"))
    return Split(seed=raw["seed"], calibration=raw["calibration"], evaluation=raw["evaluation"])


def check_split(stored: Split, fresh: Split, pinned: str = EVALUATION_SPLIT_FINGERPRINT) -> None:
    if fresh != stored:
        raise HarnessGuardError("the split recomputed from the repository differs from split.yaml")
    if stored.fingerprint != pinned:
        raise HarnessGuardError(
            f"the evaluation split's fingerprint {stored.fingerprint[:12]} is not the pinned one"
        )


def fresh_split(lock: SelectionLock | None) -> Split:
    seeded = load_scenarios()
    if lock is None:
        raise HarnessGuardError("real_selection.lock is missing")
    labels = label_all(seeded) + [c.expected for c in lock.cases]
    return make_split([*seeded, *lock.cases], labels)


def load_calibration_log(path: Path | None = None) -> list[CalibrationEntry]:
    source = path or CALIBRATION_LOG
    if not source.exists():
        raise HarnessGuardError(f"{source.name} is missing: it records the calibration cases")
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return [CalibrationEntry.model_validate(entry) for entry in raw.get("entries") or []]


def check_calibration(
    entries: list[CalibrationEntry], evaluation: list[str], policy: PolicyConfig
) -> None:
    held_out = set(evaluation)
    for entry in entries:
        used = sorted(held_out & set(entry.cases))
        if used:
            raise HarnessGuardError(
                f"{entry.parameter} was calibrated on evaluation cases {used}: "
                "the evaluation split is held out (policy §16.4)"
            )
    logged = {entry.parameter for entry in entries}
    for name in CALIBRATED_PARAMETERS:
        if getattr(policy.parameters, name) is not None and name not in logged:
            raise HarnessGuardError(
                f"{name} is set but calibration_log.yaml does not say which cases fitted it"
            )


def check_real_selection(lock: SelectionLock | None, needs_real: bool) -> None:
    if not needs_real:
        return
    if lock is None:
        raise HarnessGuardError("real_selection.lock is missing")
    local = load_real_scenarios()
    if not local:
        raise HarnessGuardError(
            "config/eval_scenarios/local/ has no real cases: run scripts/select_real_scenarios.py"
        )
    if selection_digest(local) != lock.ids_sha256:
        raise HarnessGuardError("the local real cases are not the locked selection (ids_sha256)")


def check_all(policy: PolicyConfig) -> Split:
    """Every guard; the evaluation split when they all hold."""
    lock = load_lock()
    stored = stored_split()
    check_split(stored, fresh_split(lock))
    check_calibration(load_calibration_log(), stored.evaluation, policy)
    check_real_selection(lock, any(key.startswith("R") for key in stored.evaluation))
    return stored
