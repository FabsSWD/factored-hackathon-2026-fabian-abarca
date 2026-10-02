"""M13 light load with fast doubles: the milestone's targets (p50 < 2 s, p95 < 5 s), and the
parallel calls of extract and Kev. The measured latencies run in scripts/load_test.py (about a
minute of real waiting) and are reported in docs/backend-acceptance.md."""

from __future__ import annotations

from tests.e2e.load import FAST, Profile, run_load


def test_100_turns_with_fast_doubles_meet_the_targets() -> None:
    for connect_enabled in (True, False):
        result = run_load(FAST, connect_enabled=connect_enabled)
        assert result.turns == 100
        assert result.p50 < 2.0 and result.p95 < 5.0, result
        assert result.outcomes.get("RESOLVE", 0) > 0 and "ESCALATE" not in result.outcomes


def test_extract_and_kev_run_in_parallel_and_connect_after() -> None:
    fixed = Profile("fixed", extract=(0.20, 0.20), connect=(0.10, 0.10), kev=(0.15, 0.15))
    result = run_load(fixed, conversations=3, turns_each=2)
    # In sequence a turn would take 0.45 s; in parallel max(0.20, 0.15) + 0.10 = 0.30 s.
    assert result.maximum < 0.40, result
    without_connect = run_load(fixed, connect_enabled=False, conversations=3, turns_each=2)
    assert without_connect.maximum < 0.30, without_connect
