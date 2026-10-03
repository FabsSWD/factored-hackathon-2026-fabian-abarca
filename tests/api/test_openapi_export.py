"""The frontend's OpenAPI document is the API's: its TypeScript types come from it (M14)."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_the_frontend_openapi_document_is_up_to_date() -> None:
    spec = importlib.util.spec_from_file_location(
        "export_openapi", ROOT / "scripts" / "export_openapi.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["export_openapi"] = module
    spec.loader.exec_module(module)
    committed = json.loads((ROOT / "frontend" / "openapi.json").read_text(encoding="utf-8"))
    assert committed == json.loads(module.document()), (
        "frontend/openapi.json is out of date: run python scripts/export_openapi.py and "
        "npm run gen:api in frontend/"
    )
