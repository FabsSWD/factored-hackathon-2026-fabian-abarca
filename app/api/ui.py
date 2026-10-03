"""Interface texts of the customer chat (M14): ``GET /api/ui/texts/{language}``.

Public: the login screen needs them before any session exists. They hold no account data.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Response

from app.templates.ui_texts import UiTexts, ui_texts

router = APIRouter(prefix="/api/ui", tags=["ui"])


@router.get("/texts/{language}")
def texts(language: Literal["es", "pt"], response: Response) -> UiTexts:
    response.headers["Cache-Control"] = "public, max-age=300"
    return ui_texts()[language]
