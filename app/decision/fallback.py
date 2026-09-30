"""Fallback signals derived from the LLM extraction, and the resolution rule for a turn.

When Kev is unavailable, signals are derived from the extraction of the *current message*
only, never from slots accumulated over the conversation: Kev also sees only the current
message, so both start from the same input and M18 compares them fairly.

The derived values are 0/1 and deliberately uncalibrated. This is the baseline Kev is
evaluated against, not a model:

- ``reason_code_probs``: ``{extracted code: 1.0}``, or empty when no code was extracted. An
  unstated reason is unknown, not "outside scope", so ``reason_code_other`` stays null.
- ``ambiguity``: 1.0 when neither a reason code nor a transaction reference was extracted.
- ``escalation_risk``: 1.0 when the customer asked for a human, raised a legal or vulnerability
  signal, or reported an account takeover.
"""

from __future__ import annotations

from app.contracts import ExtractionResult, ModelSignals, ModelSource

FALLBACK_VERSION = "llm_fallback@1.0.0"


def derive_fallback(extraction: ExtractionResult) -> ModelSignals:
    slots, flags = extraction.slots, extraction.flags
    reason = slots.reason_code
    urgent = (
        flags.human_requested or flags.legal_or_vulnerability or flags.account_takeover_reported
    )
    return ModelSignals(
        source=ModelSource.LLM_FALLBACK,
        model_version=FALLBACK_VERSION,
        reason_code_probs={reason: 1.0} if reason is not None else {},
        ambiguity=1.0 if reason is None and slots.transaction_ref is None else 0.0,
        escalation_risk=1.0 if urgent else 0.0,
    )


def resolve_signals(kev: ModelSignals, extraction: ExtractionResult | None) -> ModelSignals:
    """Kev when it answered; otherwise the fallback from the extraction; otherwise unavailable."""
    if kev.source is ModelSource.KEV:
        return kev
    if extraction is not None:
        return derive_fallback(extraction)
    return ModelSignals(source=ModelSource.UNAVAILABLE)
