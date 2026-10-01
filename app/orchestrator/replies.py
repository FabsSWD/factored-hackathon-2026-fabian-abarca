"""Customer replies built only from versioned templates (COM-02), with the composition rules of
``app.templates.service``:

- ``tool_failure`` is always followed by ``handoff`` (or ``handoff_unauthenticated``) or by
  ``offer_transfer``;
- every INFORM template is followed by ``offer_transfer``;
- before GATE-02 passes, the only handoff text is ``handoff_unauthenticated``, and no template
  that shows account data is used.

``Reply.check`` enforces them, so a reply that breaks a rule never reaches the customer.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.contracts import (
    CaseRecord,
    Language,
    ProductRecord,
    ReasonCode,
    TransactionRecord,
)
from app.templates.formatting import Locale, format_amount, format_date
from app.templates.service import INFORM_TEMPLATES, TemplateService

INFORM_IDS = frozenset(INFORM_TEMPLATES.values())
HANDOFF_IDS = frozenset({"handoff", "handoff_unauthenticated"})
# Templates that show account data: never before GATE-02 passes.
ACCOUNT_IDS = frozenset(
    {
        "case_created",
        "confirm_summary",
        "confirm_block_card",
        "card_blocked",
        "card_already_blocked",
        "duplicate_case",
        "choose_transaction",
        "candidate_line",
        "clarify_duplicate_ref",
        "handoff",
    }
)


class CompositionError(ValueError):
    """A reply that breaks a composition rule of COM-02."""


@dataclass
class Reply:
    templates: TemplateService
    language: Language
    locale: Locale
    ids: list[str] = field(default_factory=list)
    texts: list[str] = field(default_factory=list)

    def add(self, template_id: str, **values: object) -> None:
        self.ids.append(template_id)
        self.texts.append(self.templates.render(template_id, self.language, **values))

    # --- values -------------------------------------------------------------------------------

    def amount(self, txn: TransactionRecord) -> object:
        return format_amount(txn.amount, txn.currency, self.locale)

    def merchant(self, txn: TransactionRecord) -> str:
        return txn.merchant_name or self.templates.label("misc", "no_merchant", self.language)

    # --- composite replies ----------------------------------------------------------------------

    def summary(self, txn: TransactionRecord, product: ProductRecord, reason: ReasonCode) -> None:
        self.add(
            "confirm_summary",
            transaction_date=format_date(txn.transaction_date),
            merchant=self.merchant(txn),
            amount=self.amount(txn),
            product=product.product_number_masked,
            reason=self.templates.label("reason_code", reason.value, self.language),
        )

    def candidates(
        self, transactions: list[TransactionRecord], products: dict[str, ProductRecord]
    ) -> None:
        self.add("choose_transaction")
        days = [txn.transaction_date.date() for txn in transactions]
        same_day = len(set(days)) < len(days)  # then the time tells them apart
        for index, txn in enumerate(transactions, start=1):
            product = products.get(txn.product_id)
            when = format_date(txn.transaction_date)
            if same_day:
                when = f"{when} {txn.transaction_date:%H:%M}"
            self.add(
                "candidate_line",
                index=index,
                transaction_date=when,
                product=product.product_number_masked if product else "****0000",
                merchant=self.merchant(txn),
                amount=self.amount(txn),
            )

    def duplicate_case(self, case: CaseRecord) -> None:
        self.add(
            "duplicate_case",
            case_ref=case.case_id,
            status=self.templates.label("case_status", case.status.value, self.language),
        )
        self.add("offer_transfer")

    # --- result -------------------------------------------------------------------------------

    def check(self, authenticated: bool) -> None:
        for index, template_id in enumerate(self.ids):
            following = self.ids[index + 1 :]
            if template_id == "tool_failure" and not (
                set(following) & (HANDOFF_IDS | {"offer_transfer"})
            ):
                raise CompositionError(
                    "tool_failure must be followed by a handoff or a transfer offer"
                )
            if template_id in INFORM_IDS and "offer_transfer" not in following:
                raise CompositionError(f"{template_id} must be followed by offer_transfer")
        if not authenticated and set(self.ids) & ACCOUNT_IDS:
            raise CompositionError(
                f"account templates before authentication: {sorted(set(self.ids) & ACCOUNT_IDS)}"
            )

    @property
    def text(self) -> str:
        return "\n\n".join(self.texts)
