"""Print example handoff packets (policy §13), one per route.

    python scripts/show_handoff_example.py                 # all routes
    python scripts/show_handoff_example.py fraud           # one route

Routes: disputes, fraud, security_review, unauthenticated. The packets come from synthetic
records through the real Policy Engine and Handoff Builder (app/handoff/examples.py): no
database, no model calls. Use it to review what a human agent receives, for M15 and the demo.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.handoff.examples import ROUTES, example_packets  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("routes", nargs="*", choices=[[], *ROUTES], default=list(ROUTES))
    args = parser.parse_args()
    packets = example_packets()
    for route in args.routes or ROUTES:
        print(f"===== {route} =====")
        print(json.dumps(packets[route].model_dump(mode="json"), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
