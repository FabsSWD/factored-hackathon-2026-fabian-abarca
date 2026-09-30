"""Decision Client: Kev's typed signals, with a fallback derived from the LLM extraction."""

from app.decision.fallback import derive_fallback, resolve_signals

__all__ = ["derive_fallback", "resolve_signals"]
