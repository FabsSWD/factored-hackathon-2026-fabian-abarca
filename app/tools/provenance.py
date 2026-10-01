"""DATA-04: every verified fact names its source table and record ID.

Records read by the Tool Layer map one-to-one to a table and a primary key, so provenance is
derived from the record itself rather than carried in every contract.
"""

from __future__ import annotations

from app.contracts import (
    CaseRecord,
    CustomerRecord,
    ProductRecord,
    TransactionRecord,
    VerifiedFact,
)

Record = CustomerRecord | ProductRecord | TransactionRecord | CaseRecord


def provenance(record: Record) -> tuple[str, str]:
    """The source table and record ID of a record read through the Tool Layer."""
    if isinstance(record, CustomerRecord):
        return "customers", record.customer_id
    if isinstance(record, ProductRecord):
        return "products", record.product_id
    if isinstance(record, TransactionRecord):
        return "transactions", record.transaction_id
    return "cases", record.case_id


def verified_fact(fact: str, record: Record) -> VerifiedFact:
    source, record_id = provenance(record)
    return VerifiedFact(fact=fact, source=source, record_id=record_id)
