"""Builds the Decision Client from settings."""

from __future__ import annotations

from app.decision.client import CallRecorder, KevConfig, KevDecisionClient
from app.decision.questions import load_kev_questions
from app.settings import Settings


def decision_client_from_settings(
    settings: Settings, recorder: CallRecorder | None = None
) -> KevDecisionClient:
    """Without ``KEV_BASE_URL`` the client is unconfigured and always returns unavailable."""
    config = (
        KevConfig(
            settings.kev_base_url,
            settings.kev_timeout_seconds,
            api_key=settings.kev_api_key.get_secret_value() if settings.kev_api_key else None,
            model=settings.kev_model,
        )
        if settings.kev_base_url
        else None
    )
    return KevDecisionClient(config, load_kev_questions(), recorder=recorder)
