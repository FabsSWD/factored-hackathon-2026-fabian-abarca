"""M13 light load: 100 turns with latency doubles, with and without connecting sentences.

    python scripts/load_test.py            # measured and fast profiles (about a minute)
    python scripts/load_test.py --fast     # fast doubles only

The doubles wait for the latencies measured on the real services (extract 2.0-4.9 s, connect
1.5-2.5 s, Kev 0.2-0.4 s in parallel with extract) or for fast ones; the rest of the turn is
real code (tests/e2e/load.py). Prints p50 and p95 of the turn and writes
reports/m13_load_results.json for docs/backend-acceptance.md. No network, no database.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.e2e.load import FAST, MEASURED, run_load  # noqa: E402

REPORT = ROOT / "reports" / "m13_load_results.json"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--fast", action="store_true", help="fast doubles only")
    args = parser.parse_args()
    profiles = [FAST] if args.fast else [MEASURED, FAST]
    results = []
    print(f"{'profile':10} {'connect':8} {'turns':>5} {'p50 s':>7} {'p95 s':>7} {'max s':>7} {'wall s':>7}")
    for profile in profiles:
        for connect_enabled in (True, False):
            result = run_load(profile, connect_enabled=connect_enabled)
            results.append(asdict(result))
            print(
                f"{result.profile:10} {('on' if connect_enabled else 'off'):8} {result.turns:5d} "
                f"{result.p50:7.2f} {result.p95:7.2f} {result.maximum:7.2f} {result.wall:7.2f}"
            )
    if not args.fast:
        REPORT.write_text(json.dumps({"runs": results}, indent=2) + "\n", encoding="utf-8")
        print(f"written {REPORT.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
