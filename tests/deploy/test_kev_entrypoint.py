"""Kev container entrypoint (docker/kev/entrypoint.py): the warm-up asks what the API asks, and
the healthcheck reports ready only after the warm-up. No Kev server is started."""

from __future__ import annotations

import importlib.util
import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.decision.client import KevDecisionClient
from app.decision.questions import DEFAULT_QUESTIONS_PATH, load_kev_questions
from tests.conftest import ROOT

ENTRYPOINT = ROOT / "docker" / "kev" / "entrypoint.py"


@pytest.fixture
def entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    spec = importlib.util.spec_from_file_location("kev_entrypoint", ENTRYPOINT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "READY", tmp_path / "kev-ready")
    monkeypatch.setattr(module, "QUESTIONS", DEFAULT_QUESTIONS_PATH)
    return module


def test_warm_up_sends_the_api_request_shape(entrypoint: ModuleType) -> None:
    questions = load_kev_questions()
    client = KevDecisionClient(None, questions)
    expected = client.request_body("x")
    body = {**entrypoint._questions(), "state": "x"}
    assert body == expected


def test_warm_up_covers_both_languages(entrypoint: ModuleType) -> None:
    states = entrypoint.WARMUP_STATES
    assert len(states) == 2
    assert any("cargo" in s for s in states) and any("cobrado" in s for s in states)


def test_missing_questions_file_falls_back_to_a_generic_question(
    entrypoint: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(entrypoint, "QUESTIONS", tmp_path / "missing.yaml")
    assert entrypoint._questions() == entrypoint.FALLBACK_QUESTIONS


class _Models(BaseHTTPRequestHandler):
    status = 200
    posted: list[dict[str, Any]] = []  # noqa: RUF012

    def do_GET(self) -> None:
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(b'{"models": []}')

    def do_POST(self) -> None:
        length = int(self.headers["content-length"])
        body = json.loads(self.rfile.read(length))
        type(self).posted.append(body)
        answers: dict[str, dict[str, Any]] = {name: {} for name in body["questions"]}
        self.send_response(200)
        self.end_headers()
        self.wfile.write(json.dumps({"answers": answers}).encode())

    def log_message(self, *args: object) -> None:
        pass


@pytest.fixture
def fake_kev(entrypoint: ModuleType, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    _Models.posted = []
    server = HTTPServer(("127.0.0.1", 0), _Models)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(entrypoint, "BASE", f"http://127.0.0.1:{server.server_port}")
    try:
        yield
    finally:
        server.shutdown()
        server.server_close()


def test_healthcheck_waits_for_the_warm_up(entrypoint: ModuleType, fake_kev: None) -> None:
    assert entrypoint.healthcheck() == 1  # serving, but not warmed up
    entrypoint.READY.touch()
    assert entrypoint.healthcheck() == 0


def test_healthcheck_fails_when_kev_does_not_answer(
    entrypoint: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    entrypoint.READY.touch()
    monkeypatch.setattr(entrypoint, "BASE", "http://127.0.0.1:9")  # nothing listens
    assert entrypoint.healthcheck() == 1


def test_warm_up_posts_each_state(entrypoint: ModuleType, fake_kev: None) -> None:
    entrypoint._warm_up()
    assert [body["state"] for body in _Models.posted] == list(entrypoint.WARMUP_STATES)
    assert all(set(body["questions"]) == set(load_kev_questions().questions)
               for body in _Models.posted)  # fmt: skip
