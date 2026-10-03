"""The evaluation split (policy §16.4), fixed before any prompt, threshold or model tuning.

About a third of the cases go to ``calibration`` (M7: the only cases thresholds may be fitted
on) and the rest to ``evaluation`` (M18: never used to set a ``Calibrated`` parameter). The
split is stratified by data source (real or seeded records), language and path and shuffled
with a fixed seed; then every rule and
every failed gate keeps at least one case in evaluation (a calibration case moves over, the
lowest id first), so that every rule is evaluated.
The same cases and seed always give the same split.
"""

from __future__ import annotations

import hashlib
import random
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.evaluation.labeler import CaseLabel
from app.evaluation.scenarios import Path3

SPLIT_SEED = 20261002
CALIBRATION_SHARE = 1 / 3


class Case(Protocol):
    """What the split needs of a case: a seeded ``Scenario`` or a ``RealScenario``."""

    @property
    def id(self) -> str: ...
    @property
    def language(self) -> str: ...
    @property
    def path(self) -> Path3: ...
    @property
    def data_source(self) -> str: ...


class Split(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    seed: int
    calibration: list[str]
    evaluation: list[str]

    @property
    def fingerprint(self) -> str:
        """sha256 of both lists: a test pins it, so any change to the split fails loudly."""
        text = (
            "calibration:" + ",".join(self.calibration) + ";evaluation:" + ",".join(self.evaluation)
        )
        return hashlib.sha256(text.encode()).hexdigest()


def _rules(label: CaseLabel) -> set[str]:
    return set(label.triggered_rules) | {g for d in label.disputes for g in d.failed_gates}


def make_split(scenarios: Sequence[Case], labels: list[CaseLabel], seed: int = SPLIT_SEED) -> Split:
    by_id = {label.case_id: label for label in labels}
    counts = Counter(rule for label in labels for rule in _rules(label))
    forced = {
        scenario.id
        for scenario in scenarios
        if any(counts[r] == 1 for r in _rules(by_id[scenario.id]))
    }
    strata: dict[tuple[str, str, str], list[str]] = defaultdict(list)
    for scenario in sorted(scenarios, key=lambda s: s.id):
        strata[(scenario.data_source, scenario.language, scenario.path.value)].append(scenario.id)
    rng = random.Random(seed)
    calibration: list[str] = []
    for key in sorted(strata):
        free = [case_id for case_id in strata[key] if case_id not in forced]
        rng.shuffle(free)
        calibration += free[: round(len(strata[key]) * CALIBRATION_SHARE)]
    for rule in sorted(counts):  # every rule and failed gate is evaluated at least once
        cases = sorted(s.id for s in scenarios if rule in _rules(by_id[s.id]))
        if all(case_id in calibration for case_id in cases):
            calibration.remove(cases[0])
    evaluation = [s.id for s in scenarios if s.id not in set(calibration)]
    return Split(seed=seed, calibration=sorted(calibration), evaluation=sorted(evaluation))
