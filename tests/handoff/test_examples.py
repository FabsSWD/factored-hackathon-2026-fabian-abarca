"""The example packets (app/handoff/examples.py) and scripts/show_handoff_example.py."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from app.contracts import PROBABILITY_SUM_TOLERANCE, HandoffPacket, ModelSource
from app.handoff.examples import ROUTES, example_packets

ROOT = Path(__file__).resolve().parents[2]
URGENT_RULES = {"ESC-03", "ESC-05", "ESC-06"}  # the flags that make the fallback urgent


def run_script(*routes: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "show_handoff_example.py"), *routes],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        check=False,
    )


def printed(stdout: str) -> dict[str, HandoffPacket]:
    packets: dict[str, HandoffPacket] = {}
    for block in stdout.split("===== ")[1:]:
        route, _, body = block.partition(" =====\n")
        packets[route] = HandoffPacket.model_validate(json.loads(body))
    return packets


def test_script_without_arguments_prints_every_route() -> None:
    result = run_script()
    assert result.returncode == 0, result.stderr
    assert tuple(printed(result.stdout)) == ROUTES


def test_script_with_one_route() -> None:
    result = run_script("fraud")
    assert result.returncode == 0, result.stderr
    assert tuple(printed(result.stdout)) == ("fraud",)


def test_script_rejects_an_unknown_route() -> None:
    result = run_script("sales")
    assert result.returncode != 0
    assert "unknown route(s): sales" in result.stderr


def test_fallback_signals_follow_the_derivation_rule() -> None:
    # derive_fallback: reason probabilities 0/1 from the extracted reason code; escalation risk
    # 1.0 exactly when an urgent flag (human, legal or vulnerability, takeover) was raised.
    fallback = [
        packet
        for packet in example_packets().values()
        if packet.model_signals.source is ModelSource.LLM_FALLBACK
    ]
    assert fallback
    for packet in fallback:
        signals = packet.model_signals
        assert set(signals.reason_code_probs.values()) <= {1.0}
        urgent = bool(URGENT_RULES & set(packet.triggered_rules))
        assert signals.escalation_risk == (1.0 if urgent else 0.0)


def test_at_least_one_example_uses_the_fallback() -> None:
    sources = {p.model_signals.source for p in example_packets().values()}
    assert {ModelSource.KEV, ModelSource.LLM_FALLBACK, ModelSource.UNAVAILABLE} <= sources


def test_kev_signals_are_complete_distributions() -> None:
    for packet in example_packets().values():
        signals = packet.model_signals
        if signals.source is not ModelSource.KEV:
            continue
        assert len(signals.reason_code_probs) == 5
        assert signals.reason_code_other is not None
        total = sum(signals.reason_code_probs.values()) + signals.reason_code_other
        assert abs(total - 1.0) <= PROBABILITY_SUM_TOLERANCE


def test_kev_signals_match_the_reason_the_customer_gave() -> None:
    for packet in example_packets().values():
        signals = packet.model_signals
        if signals.source is ModelSource.KEV and packet.reason_code is not None:
            top = max(signals.reason_code_probs, key=lambda code: signals.reason_code_probs[code])
            assert top is packet.reason_code


def test_flagged_message_has_no_model_signals() -> None:
    # ESC-13: the flagged message reached neither the LLM nor Kev.
    signals = example_packets()["security_review"].model_signals
    assert signals.source is ModelSource.UNAVAILABLE
    assert signals.reason_code_probs == {} and signals.escalation_risk is None
