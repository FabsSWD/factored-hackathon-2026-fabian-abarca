"""Kev container entrypoint: serve, warm up, then report ready.

    kev-entrypoint.py                 # start kev.serve on KEV_RUN and warm it up
    kev-entrypoint.py --healthcheck   # exit 0 once warmed up and answering GET /v1/models

The first call after start compiles the model (about 10 s on a GPU, longer on CPU), so the
container makes warm-up calls before it reports healthy. They use the questions the API sends
(config/kev_questions.yaml, mounted at KEV_WARMUP_QUESTIONS), in Spanish and Portuguese, so the
first customer turn does not pay for the compilation. SIGTERM and SIGINT reach the server.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

import yaml

READY = Path("/tmp/kev-ready")
PORT = int(os.environ.get("KEV_PORT", "8008"))
BASE = f"http://127.0.0.1:{PORT}"
QUESTIONS = Path(os.environ.get("KEV_WARMUP_QUESTIONS", "/etc/kev/questions.yaml"))
STARTUP_TIMEOUT = float(os.environ.get("KEV_STARTUP_TIMEOUT_SECONDS", "900"))
WARMUP_STATES = (
    "No reconozco un cargo de 45 dólares en mi tarjeta de ayer, yo no hice esa compra.",
    "Fui cobrado duas vezes pela mesma compra no supermercado, quero contestar uma delas.",
)
FALLBACK_QUESTIONS: dict[str, Any] = {
    "model": "kev-latest",
    "questions": {"warmup": {"type": "noul", "instructions": "Is this a dispute?"}},
}


def _get(path: str, timeout: float) -> int:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as response:
        return int(response.status)


def _post(path: str, body: dict[str, Any], timeout: float) -> dict[str, Any]:
    request = urllib.request.Request(
        BASE + path,
        data=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return dict(json.load(response))


def healthcheck() -> int:
    if not READY.exists():
        return 1
    try:
        return 0 if _get("/v1/models", timeout=4) == 200 else 1
    except (urllib.error.URLError, OSError):
        return 1


def _questions() -> dict[str, Any]:
    if not QUESTIONS.is_file():
        print(f"[kev] {QUESTIONS} not found: warming up with a generic question", flush=True)
        return FALLBACK_QUESTIONS
    loaded = yaml.safe_load(QUESTIONS.read_text(encoding="utf-8"))
    return {"model": loaded["model"], "questions": loaded["questions"]}


def _wait_until_serving(server: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if server.poll() is not None:
            sys.exit(f"[kev] server exited with {server.returncode} before serving")
        try:
            if _get("/v1/models", timeout=2) == 200:
                return
        except (urllib.error.URLError, OSError):
            pass
        time.sleep(1)
    server.terminate()
    sys.exit(f"[kev] server not serving after {STARTUP_TIMEOUT:.0f} s")


def _warm_up() -> None:
    questions = _questions()
    for state in WARMUP_STATES:
        started = time.monotonic()
        answer = _post("/v1/systemone", {**questions, "state": state}, timeout=STARTUP_TIMEOUT)
        elapsed = time.monotonic() - started
        print(
            f"[kev] warm-up answered {sorted(answer.get('answers', {}))} in {elapsed:.1f} s",
            flush=True,
        )


def serve() -> int:
    READY.unlink(missing_ok=True)
    run = os.environ["KEV_RUN"]
    server = subprocess.Popen(
        [sys.executable, "-m", "kev.serve", "--run", run, "--host", "0.0.0.0", "--port", str(PORT)],
        cwd="/kev",
    )

    def forward(signum: int, _frame: object) -> None:
        server.send_signal(signum)

    signal.signal(signal.SIGTERM, forward)
    signal.signal(signal.SIGINT, forward)

    _wait_until_serving(server)
    try:
        _warm_up()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        server.terminate()
        server.wait()
        sys.exit(f"[kev] warm-up failed: {exc}")
    READY.touch()
    print("[kev] ready", flush=True)
    return server.wait()


if __name__ == "__main__":
    sys.exit(healthcheck() if "--healthcheck" in sys.argv[1:] else serve())
