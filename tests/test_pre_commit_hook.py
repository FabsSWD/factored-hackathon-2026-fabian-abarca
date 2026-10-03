"""The pre-commit hook (scripts/hooks/pre-commit) blocks a commit whose staged files hold an
identifier, in a throwaway repository with the hook installed as the README says."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures.core_banking import CUSTOMER

ROOT = Path(__file__).resolve().parent.parent
EVAL = Path("config") / "eval_scenarios"


def git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k != "IDENTIFIER_CHECK_ROOT"}
    env |= {"HOOK_PYTHON": sys.executable, "GITLEAKS": "gitleaks-not-installed"}
    return subprocess.run(
        ["git", "-c", "user.name=hook", "-c", "user.email=hook@example.com", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        check=False,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The files the identifier test reads, the test itself and the hook."""
    for report in (ROOT / "reports").glob("*.json"):
        target = tmp_path / "reports" / report.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(report, target)
    shutil.copytree(ROOT / EVAL, tmp_path / EVAL, ignore=shutil.ignore_patterns("local"))
    (tmp_path / "tests").mkdir()
    shutil.copy(ROOT / "tests" / "test_reports.py", tmp_path / "tests" / "test_reports.py")
    hook = tmp_path / "scripts" / "hooks" / "pre-commit"
    hook.parent.mkdir(parents=True)
    shutil.copy(ROOT / "scripts" / "hooks" / "pre-commit", hook)
    hook.chmod(0o755)
    assert git(tmp_path, "init", "-q").returncode == 0
    assert git(tmp_path, "config", "core.hooksPath", "scripts/hooks").returncode == 0
    assert git(tmp_path, "add", "-A").returncode == 0
    first = git(tmp_path, "commit", "-q", "-m", "clean")
    assert first.returncode == 0, first.stdout + first.stderr  # clean files pass the hook
    assert "gitleaks is not installed" in first.stderr  # skipped with a warning
    return tmp_path


def commits(repo: Path) -> int:
    return int(git(repo, "rev-list", "--count", "HEAD").stdout)


def test_a_staged_customer_id_blocks_the_commit(repo: Path) -> None:
    leak = repo / EVAL / "leak.yaml"
    leak.write_text(f"cases:\n- customer_id: {CUSTOMER}\n", encoding="utf-8")
    git(repo, "add", str(leak.relative_to(repo)))
    result = git(repo, "commit", "-q", "-m", "leak")
    assert result.returncode != 0
    assert "BLOCKED" in result.stderr
    assert commits(repo) == 1


def test_the_hook_checks_what_is_staged(repo: Path) -> None:
    report = repo / "reports" / "leak.json"
    report.write_text(f'{{"sample": "{CUSTOMER}"}}', encoding="utf-8")
    git(repo, "add", "reports/leak.json")
    report.unlink()  # gone from the working tree, still in the commit
    assert git(repo, "commit", "-q", "-m", "leak").returncode != 0
    assert commits(repo) == 1


def test_a_clean_change_commits(repo: Path) -> None:
    (repo / EVAL / "notes.yaml").write_text("note: aggregate figures only\n", encoding="utf-8")
    git(repo, "add", "-A")
    result = git(repo, "commit", "-q", "-m", "clean change")
    assert result.returncode == 0, result.stdout + result.stderr
    assert commits(repo) == 2
