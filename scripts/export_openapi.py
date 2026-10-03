"""Write the API's OpenAPI document for the frontend, which generates its TypeScript types from it.

    python scripts/export_openapi.py          # writes frontend/openapi.json
    cd frontend && npm run gen:api            # regenerates src/api/schema.d.ts

A test (tests/api/test_openapi_export.py) fails when frontend/openapi.json is out of date, so a
change in a response the chat reads cannot go unnoticed by the frontend.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import load_policy_config  # noqa: E402
from app.main import create_app  # noqa: E402

TARGET = ROOT / "frontend" / "openapi.json"


def document() -> str:
    spec = create_app(load_policy_config()).openapi()
    return json.dumps(spec, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> None:
    TARGET.parent.mkdir(exist_ok=True)
    TARGET.write_text(document(), encoding="utf-8", newline="\n")
    print(f"written {TARGET.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
