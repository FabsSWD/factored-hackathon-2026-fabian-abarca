"""Builds the LLM Adapter from settings."""

from __future__ import annotations

from app.llm_adapter.adapter import OpenAILLMAdapter
from app.llm_adapter.client import CallRecorder, LLMClientConfig, OpenAIJsonClient
from app.settings import Settings


def adapter_from_settings(
    settings: Settings, recorder: CallRecorder | None = None
) -> OpenAILLMAdapter:
    key = settings.openai_api_key.get_secret_value() if settings.openai_api_key else ""
    config = LLMClientConfig(
        api_key=key,
        model=settings.llm_model or "",
        base_url=settings.openai_base_url,
        timeout_seconds=settings.llm_timeout_seconds,
        max_retries=settings.llm_max_retries,
        turn_deadline_seconds=settings.llm_turn_deadline_seconds,
    )
    return OpenAILLMAdapter(
        OpenAIJsonClient(config, recorder=recorder),
        connect_enabled=settings.llm_connect_enabled,
    )
