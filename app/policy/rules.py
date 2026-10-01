"""Static rule tables of docs/dispute-policy.md: disputability (§4), routes (§7), and the
names ``explain()`` uses (principle 5)."""

from __future__ import annotations

from enum import StrEnum

from app.contracts import ActionId, Priority, Queue, ReasonCode


class Mark(StrEnum):
    """Policy §4 legend."""

    AUTOMATED = "A"
    HUMAN = "H"  # ESC-14
    NOT_DISPUTABLE = "N"  # INFORM with an offer to transfer


_A, _H, _N = Mark.AUTOMATED, Mark.HUMAN, Mark.NOT_DISPUTABLE
_COLUMNS = (
    ReasonCode.UNRECOGNIZED,
    ReasonCode.DUPLICATE,
    ReasonCode.INCORRECT_AMOUNT,
    ReasonCode.NOT_RECEIVED,
    ReasonCode.FEE,
)
_ROWS: dict[str, tuple[Mark, Mark, Mark, Mark, Mark]] = {
    "Purchase": (_A, _A, _A, _A, _N),
    "Withdrawal": (_A, _H, _H, _H, _N),
    "Transfer": (_H, _H, _N, _N, _N),
    "Payment": (_H, _H, _N, _N, _N),
    "Deposit": (_N, _N, _N, _N, _N),
    "Adjustment": (_N, _A, _N, _N, _A),
}
DISPUTABILITY: dict[str, dict[ReasonCode, Mark]] = {
    transaction_type: dict(zip(_COLUMNS, marks, strict=True))
    for transaction_type, marks in _ROWS.items()
}
"""Policy §4: ``DISPUTABILITY[transaction_type][reason_code]``."""

ACCOUNT_INITIATED_TYPES = frozenset({"Transfer", "Payment"})  # ESC-14 fraud route

QUEUE_RANK: dict[Queue, int] = {Queue.DISPUTES: 0, Queue.FRAUD: 1, Queue.SECURITY_REVIEW: 2}
"""§7, several triggers at once: security review > fraud > disputes."""

ROUTES: dict[str, tuple[Queue, Priority]] = {
    "ESC-01": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-02": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-03": (Queue.FRAUD, Priority.HIGH),
    "ESC-04": (Queue.FRAUD, Priority.NORMAL),
    "ESC-05": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-06": (Queue.DISPUTES, Priority.HIGH),
    "ESC-07": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-08": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-09": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-10": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-11": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-12": (Queue.DISPUTES, Priority.NORMAL),
    "ESC-13": (Queue.SECURITY_REVIEW, Priority.NORMAL),
    # Fraud queue for RC_UNRECOGNIZED on transfers and payments (resolved by the engine).
    "ESC-14": (Queue.DISPUTES, Priority.NORMAL),
}
"""§7 route of each trigger."""

GATE_NAMES: dict[str, str] = {
    "GATE-01": "Supported language",
    "GATE-02": "Authenticated session",
    "GATE-03": "Customer status",
    "GATE-04": "Ownership",
    "GATE-05": "Transaction identified",
    "GATE-06": "Transaction status",
    "GATE-07": "Disputable combination",
    "GATE-08": "Filing window",
    "GATE-09": "Product status",
    "GATE-10": "Reason-specific preconditions",
    "GATE-11": "No duplicate case",
}

RULE_NAMES: dict[str, str] = {
    "ESC-01": "High amount",
    "ESC-02": "Dispute velocity",
    "ESC-03": "Account takeover indicators",
    "ESC-04": "Fraud score",
    "ESC-05": "Human requested",
    "ESC-06": "Legal, regulatory, or vulnerability signals",
    "ESC-07": "Late filing",
    "ESC-08": "Ineligible status",
    "ESC-09": "Unresolved ambiguity",
    "ESC-10": "Tool failure",
    "ESC-11": "Model uncertainty (soft)",
    "ESC-12": "Unsupported language",
    "ESC-13": "Manipulation attempts",
    "ESC-14": "Plausible but unsupported dispute",
}

ACTION_NAMES: dict[ActionId, str] = {
    ActionId.READ_OWN_DATA: "Read the customer's own data",
    ActionId.CREATE_CASE: "Create a dispute case",
    ActionId.BLOCK_CARD: "Temporarily block a card",
    ActionId.RECORD_CREDIT_FLAG: "Record the provisional credit eligibility flag",
    ActionId.TRANSFER_TO_HUMAN: "Transfer to a human with a handoff packet",
}
