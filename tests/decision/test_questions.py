from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.decision.questions import (
    DEFAULT_QUESTIONS_PATH,
    REASON_CHOICES,
    KevQuestionsError,
    load_kev_questions,
)


def test_default_questions() -> None:
    questions = load_kev_questions()
    assert questions.version == "1.0.0"
    assert questions.model == "kev-latest"
    assert set(questions.questions) == {"reason_code", "ambiguous", "escalation_risk"}
    assert set(questions.questions["reason_code"]["criteria"]) == REASON_CHOICES
    assert questions.prompt_version == "kev_questions@1.0.0"
    assert re.fullmatch(r"sha256:[0-9a-f]{16}", questions.prompt_hash)


def test_human_requested_and_manipulation_are_not_asked() -> None:
    names = set(load_kev_questions().questions)
    assert "human_requested" not in names
    assert "manipulation" not in names


def _write(tmp_path: Path, data: Any) -> Path:
    path = tmp_path / "kev_questions.yaml"
    path.write_text(yaml.safe_dump(data), encoding="utf-8")
    return path


def _default() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(DEFAULT_QUESTIONS_PATH.read_text(encoding="utf-8"))
    return data


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d.update({"questions_version": 1}), "questions_version"),
        (lambda d: d.update({"model": ""}), "model"),
        (lambda d: d["questions"].pop("ambiguous"), "questions must be exactly"),
        (lambda d: d["questions"].update({"human_requested": {"type": "noul"}}), "exactly"),
        (lambda d: d["questions"]["ambiguous"].update({"type": "choice"}), "noul question"),
        (lambda d: d["questions"]["escalation_risk"].update({"instructions": ""}), "instructions"),
        (lambda d: d["questions"]["reason_code"]["criteria"].pop("OTHER"), "criteria"),
    ],
)
def test_invalid_questions_fail_at_load(tmp_path: Path, change: Any, message: str) -> None:
    data = _default()
    change(data)
    with pytest.raises(KevQuestionsError, match=message):
        load_kev_questions(_write(tmp_path, data))


def test_prompt_hash_changes_with_the_questions(tmp_path: Path) -> None:
    data = _default()
    data["questions"]["ambiguous"]["instructions"] = "Is anything unclear?"
    assert (
        load_kev_questions(_write(tmp_path, data)).prompt_hash != load_kev_questions().prompt_hash
    )


def test_missing_invalid_or_non_mapping_file(tmp_path: Path) -> None:
    with pytest.raises(KevQuestionsError, match="not found"):
        load_kev_questions(tmp_path / "absent.yaml")
    broken = tmp_path / "broken.yaml"
    broken.write_text("questions: [unclosed", encoding="utf-8")
    with pytest.raises(KevQuestionsError, match="not valid YAML"):
        load_kev_questions(broken)
    listed = tmp_path / "list.yaml"
    listed.write_text("- a\n", encoding="utf-8")
    with pytest.raises(KevQuestionsError, match="mapping"):
        load_kev_questions(listed)
