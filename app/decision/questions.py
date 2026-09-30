"""Kev's closed questions, loaded from ``config/kev_questions.yaml`` and validated at load."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.contracts import ReasonCode

DEFAULT_QUESTIONS_PATH = (
    Path(__file__).resolve().parent.parent.parent / "config" / "kev_questions.yaml"
)
OTHER = "OTHER"
REASON_CHOICES = frozenset([*(code.value for code in ReasonCode), OTHER])
QUESTION_TYPES = {"reason_code": "choice", "ambiguous": "noul", "escalation_risk": "noul"}


class KevQuestionsError(ValueError):
    """The questions file is missing or does not match what the Decision Client expects."""


@dataclass(frozen=True)
class KevQuestions:
    version: str
    model: str
    questions: dict[str, Any]

    @property
    def prompt_version(self) -> str:
        return f"kev_questions@{self.version}"

    @property
    def prompt_hash(self) -> str:
        digest = hashlib.sha256(json.dumps(self.questions, sort_keys=True).encode()).hexdigest()
        return f"sha256:{digest[:16]}"


def load_kev_questions(path: Path | str | None = None) -> KevQuestions:
    resolved = Path(path) if path is not None else DEFAULT_QUESTIONS_PATH
    try:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise KevQuestionsError(f"Kev questions file not found: {resolved}") from exc
    except yaml.YAMLError as exc:
        raise KevQuestionsError(f"Kev questions file is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise KevQuestionsError("Kev questions file must contain a mapping")
    version, model, questions = raw.get("questions_version"), raw.get("model"), raw.get("questions")
    if not isinstance(version, str) or version.count(".") != 2:
        raise KevQuestionsError("questions_version must be a string like 1.0.0")
    if not isinstance(model, str) or not model:
        raise KevQuestionsError("model must be a non-empty string")
    if not isinstance(questions, dict) or set(questions) != set(QUESTION_TYPES):
        raise KevQuestionsError(f"questions must be exactly {sorted(QUESTION_TYPES)}")
    for name, kind in QUESTION_TYPES.items():
        question = questions[name]
        if not isinstance(question, dict) or question.get("type") != kind:
            raise KevQuestionsError(f"{name} must be a {kind} question")
        if not isinstance(question.get("instructions"), str) or not question["instructions"]:
            raise KevQuestionsError(f"{name} needs instructions")
    criteria = questions["reason_code"].get("criteria")
    if not isinstance(criteria, dict) or set(criteria) != REASON_CHOICES:
        raise KevQuestionsError(f"reason_code criteria must be exactly {sorted(REASON_CHOICES)}")
    return KevQuestions(version, model, questions)
