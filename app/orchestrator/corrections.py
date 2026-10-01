"""Corrections after a declined summary (policy §8), a contract for the Orchestrator (M12).

When the customer declines the COM-03 summary ("no, el monto está mal, eran 40"), the
extraction of that reply is a proposed correction, not a fact:

- A ``reason_code`` different from the established one never replaces it silently: the turn is
  a CLARIFY with ``clarify_reason_code`` and nothing else is applied until the customer answers.
- An amount in a declined reply without a change of reason corrects
  ``transaction_ref.amount``, not ``expected_amount``: the transaction identified was not the
  right one, so GATE-05 matches again (the transaction ID is dropped).
- Any other new slot is applied as a candidate for the Policy Engine to validate.
- ``confirmation`` is cleared, so the summary is shown again and a stale yes never confirms it.

Found with the recorded answer ``tests/fixtures/llm/es_confirm_declined_amount.json``: the model
returned ``reason_code: RC_INCORRECT_AMOUNT`` and ``expected_amount: 40``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app.contracts import ClarifyTarget, Slots, TransactionRecord, TransactionRef

_CORRECTABLE = (
    "card_in_possession",
    "shared_credentials",
    "duplicate_ref",
    "expected_delivery_date",
    "merchant_contacted",
    "fee_ref",
)


@dataclass(frozen=True)
class Correction:
    """The slots to evaluate next, and whether the turn must ask for the reason first."""

    slots: Slots
    clarify_target: ClarifyTarget | None = None  # REASON_CODE when a new reason was proposed
    rematch: bool = False  # transaction_ref changed: GATE-05 matches again


def apply_declined_correction(
    established: Slots,
    proposed: Slots,
    identified: TransactionRecord | None = None,
) -> Correction:
    """Apply the extraction of a declined reply (``proposed``) to the conversation's slots.

    ``identified`` is the transaction the summary showed; its date and merchant keep narrowing
    the new match when the established reference was only an ID."""
    if proposed.reason_code is not None and proposed.reason_code != established.reason_code:
        return Correction(
            slots=established.model_copy(update={"confirmation": None}),
            clarify_target=ClarifyTarget.REASON_CODE,
        )

    updates: dict[str, object] = {"confirmation": None}
    amount = _corrected_amount(proposed)
    ref_changes = _ref_changes(proposed.transaction_ref, amount)
    if ref_changes:
        updates["transaction_ref"] = _rematch_ref(
            established.transaction_ref, identified, ref_changes
        )
    for name in _CORRECTABLE:
        value = getattr(proposed, name)
        if value is not None:
            updates[name] = value
    return Correction(slots=established.model_copy(update=updates), rematch=bool(ref_changes))


def _corrected_amount(proposed: Slots) -> Decimal | None:
    """The amount the customer gave: in a declined reply it always refers to the transaction."""
    if proposed.transaction_ref is not None and proposed.transaction_ref.amount is not None:
        return proposed.transaction_ref.amount
    return proposed.expected_amount


def _ref_changes(ref: TransactionRef | None, amount: Decimal | None) -> dict[str, object]:
    changes: dict[str, object] = {}
    if ref is not None:
        if ref.transaction_date is not None:
            changes["transaction_date"] = ref.transaction_date
        if ref.merchant is not None:
            changes["merchant"] = ref.merchant
    if amount is not None:
        changes["amount"] = amount
    return changes


def _rematch_ref(
    established: TransactionRef | None,
    identified: TransactionRecord | None,
    changes: dict[str, object],
) -> TransactionRef:
    base: dict[str, object] = {}
    if identified is not None:
        base["transaction_date"] = identified.transaction_date.date()
        if identified.merchant_name:
            base["merchant"] = identified.merchant_name
    if established is not None:
        base.update(
            {
                key: value
                for key, value in established.model_dump(exclude={"transaction_id"}).items()
                if value is not None
            }
        )
    base.update(changes)
    base.pop("transaction_id", None)  # the identified transaction was not the right one
    return TransactionRef.model_validate(base)
