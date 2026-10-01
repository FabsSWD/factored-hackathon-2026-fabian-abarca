"""Read functions over Core Banking and Cases, used by the Tool Layer (M9).

Every query takes the customer ID explicitly and filters by it, so a query can never return
another customer's record. A record that belongs to someone else and a record that does not
exist look the same to the caller (``None`` or an empty list), as GATE-04 requires. Results are
contract models: prohibited fields cannot leave this layer because the contracts have no place
for them.

A card blocked through ACT-03 is recorded in ``card_blocks`` (Core Banking is read-only for the
application), so product reads report ``product_status = 'Blocked'`` while a block exists.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from app.contracts import (
    CaseRecord,
    CaseStatus,
    CustomerRecord,
    ProductRecord,
    ProvisionalCreditFlag,
    ReasonCode,
    Tier,
    TransactionRecord,
)
from app.storage.models import CardBlock, Case, Customer, Product, Transaction


class CoreBankingRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    # --- customers and products ---------------------------------------------

    def get_customer(self, customer_id: str) -> CustomerRecord | None:
        row = self._session.get(Customer, customer_id)
        if row is None:
            return None
        return CustomerRecord(customer_id=row.customer_id, customer_status=row.customer_status)

    def get_customer_country(self, customer_id: str) -> str | None:
        """Presentation only (number format of templates). Kept out of CustomerRecord so the
        country can never reach the Policy Engine (DATA-02)."""
        return self._session.scalar(
            select(Customer.country).where(Customer.customer_id == customer_id)
        )

    def list_products(self, customer_id: str) -> list[ProductRecord]:
        query = _products().where(Product.customer_id == customer_id).order_by(Product.product_id)
        return [_product(row, blocked) for row, blocked in self._session.execute(query)]

    def get_product(self, customer_id: str, product_id: str) -> ProductRecord | None:
        query = _products().where(
            Product.customer_id == customer_id, Product.product_id == product_id
        )
        found = self._session.execute(query).first()
        return _product(*found) if found is not None else None

    # --- transactions ---------------------------------------------------------

    def get_transaction(self, customer_id: str, transaction_id: str) -> TransactionRecord | None:
        row = self._session.scalar(
            select(Transaction).where(
                Transaction.customer_id == customer_id,
                Transaction.transaction_id == transaction_id,
            )
        )
        return _transaction(row) if row is not None else None

    def find_transactions(
        self,
        customer_id: str,
        *,
        on_date: date | None = None,
        date_tolerance_days: int = 0,
        start: datetime | None = None,
        end: datetime | None = None,
        amount: Decimal | None = None,
        currency: str | None = None,
        merchant_contains: str | None = None,
        merchant_name: str | None = None,
        product_id: str | None = None,
        transaction_type: str | None = None,
        transaction_status: str | None = None,
        exclude_transaction_id: str | None = None,
        limit: int | None = None,
    ) -> list[TransactionRecord]:
        """Transactions of one customer matching every filter given, newest first.

        ``on_date`` with ``date_tolerance_days`` matches calendar dates of the naive
        ``transaction_date`` within that many days (policy §10 uses ±1). ``start`` is
        inclusive and ``end`` exclusive.
        """
        if date_tolerance_days < 0:
            raise ValueError("date_tolerance_days must not be negative")
        query = select(Transaction).where(Transaction.customer_id == customer_id)
        if on_date is not None:
            first = datetime.combine(
                on_date - timedelta(days=date_tolerance_days), datetime.min.time()
            )
            after_last = datetime.combine(
                on_date + timedelta(days=date_tolerance_days + 1), datetime.min.time()
            )
            query = query.where(
                Transaction.transaction_date >= first, Transaction.transaction_date < after_last
            )
        if start is not None:
            query = query.where(Transaction.transaction_date >= start)
        if end is not None:
            query = query.where(Transaction.transaction_date < end)
        if amount is not None:
            query = query.where(Transaction.amount == amount)
        if currency is not None:
            query = query.where(Transaction.currency == currency)
        if merchant_contains is not None:
            query = query.where(
                Transaction.merchant_name.icontains(merchant_contains, autoescape=True)
            )
        if merchant_name is not None:
            query = query.where(Transaction.merchant_name == merchant_name)
        if product_id is not None:
            query = query.where(Transaction.product_id == product_id)
        if transaction_type is not None:
            query = query.where(Transaction.transaction_type == transaction_type)
        if transaction_status is not None:
            query = query.where(Transaction.transaction_status == transaction_status)
        if exclude_transaction_id is not None:
            query = query.where(Transaction.transaction_id != exclude_transaction_id)
        query = query.order_by(
            Transaction.transaction_date.desc(), Transaction.transaction_id
        ).limit(limit)
        return [_transaction(row) for row in self._session.scalars(query)]

    # --- cases ----------------------------------------------------------------

    def list_cases(self, customer_id: str, transaction_id: str | None = None) -> list[CaseRecord]:
        query = select(Case).where(Case.customer_id == customer_id)
        if transaction_id is not None:
            query = query.where(Case.transaction_id == transaction_id)
        query = query.order_by(Case.created_at, Case.case_id)
        return [_case(row) for row in self._session.scalars(query)]


def _products() -> Select[Product, bool]:
    blocked = CardBlock.product_id.is_not(None).label("blocked")
    return select(Product, blocked).outerjoin(CardBlock, CardBlock.product_id == Product.product_id)


def _product(row: Product, blocked: bool) -> ProductRecord:
    return ProductRecord(
        product_id=row.product_id,
        customer_id=row.customer_id,
        product_type=row.product_type,
        product_number_masked=f"****{row.product_number_last4}",
        currency=row.currency,
        product_status="Blocked" if blocked else row.product_status,
    )


def _transaction(row: Transaction) -> TransactionRecord:
    return TransactionRecord(
        transaction_id=row.transaction_id,
        customer_id=row.customer_id,
        product_id=row.product_id,
        transaction_type=row.transaction_type,
        transaction_status=row.transaction_status,
        transaction_date=row.transaction_date,
        amount=row.amount,
        currency=row.currency,
        amount_usd=row.amount_usd,
        merchant_name=row.merchant_name,
        fraud_score=float(row.fraud_score) if row.fraud_score is not None else None,
    )


def _case(row: Case) -> CaseRecord:
    return CaseRecord(
        case_id=row.case_id,
        customer_id=row.customer_id,
        transaction_id=row.transaction_id,
        reason_code=ReasonCode(row.reason_code),
        status=CaseStatus(row.status),
        tier=Tier(row.tier),
        amount=row.amount,
        currency=row.currency,
        amount_usd=row.amount_usd,
        provisional_credit_flag=(
            ProvisionalCreditFlag(row.provisional_credit_flag)
            if row.provisional_credit_flag is not None
            else None
        ),
        created_at=row.created_at,
        business_created_at=row.business_created_at,
    )
