"""Start the API server with exactly one worker.

    python scripts/serve.py                  # http://127.0.0.1:8000
    python scripts/serve.py --port 8080 --host 0.0.0.0

The Orchestrator keeps conversation state in memory (app/orchestrator/state.py): with several
workers each one would have its own state, and a conversation would lose its slots whenever a
turn reached another worker. So the server always runs with one worker; M19 keeps this until
the state moves to a shared store. A restart loses open conversations.
"""

from __future__ import annotations

import argparse

import uvicorn

WORKERS = 1  # never more while conversation state is in memory


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    uvicorn.run("app.main:app", host=args.host, port=args.port, workers=WORKERS)


if __name__ == "__main__":
    main()
