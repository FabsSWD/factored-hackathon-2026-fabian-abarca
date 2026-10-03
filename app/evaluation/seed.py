"""Seed the M17 scenarios into the database as ``SEED-`` rows (scripts/seed_scenarios.py).

- Only the owner role writes Core Banking: ``require_owner_url`` refuses a missing
  ``MIGRATION_DATABASE_URL`` or one with the application's user, and ``require_owner`` refuses
  a connection whose user does not own the tables.
- Every row is synthetic and carries the ``SEED-`` prefix; nothing here reads or writes another
  row. Lineage: ``source_file = seed:scenarios@<version>``.
- Each customer gets a known document number (``SEED-0007``) stored as its HMAC, so M18 can log
  in through the real Identity Service.
- Customers are spread over the countries and age bands of the supplied data (M18 segments).
- Seeding is idempotent and puts every case back in the state of its specification: the cases,
  card blocks, handoffs and OTP challenges left by earlier runs on ``SEED-`` customers are
  removed and their sessions are revoked (never deleted), then the rows of the specification
  are upserted. ``audit_logs`` is never touched: the audit trail is append-only.
- ``remove`` deletes every ``SEED-`` row, and nothing else. It refuses when audit records point
  to sessions of ``SEED-`` customers, because deleting those sessions would change the trail.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Table, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.config import PolicyConfig, load_policy_config
from app.evaluation.labeler import tier_of
from app.evaluation.scenarios import SEED_PREFIX, Scenario, transaction_moment
from app.storage.data_contract import document_hash
from app.storage.models import (
    AuditLog,
    CardBlock,
    Case,
    Customer,
    HandoffPacketRow,
    OtpChallenge,
    OtpFailure,
    Product,
    SessionRow,
    Transaction,
)

SEED_LIKE = f"{SEED_PREFIX}%"
ACCENTS = {"Colombia": "colombian", "México": "mexican", "Argentina": "argentine"}
CHANNELS = {"Withdrawal": "ATM", "Transfer": "App", "Payment": "App"}
CREDIT_FLAGS = {"T1": "eligible", "T2": "requires_review", "T3": "requires_review"}


def _usd_source(currency: str, usd: Decimal | None) -> str:
    if currency == "USD":
        return "identity"
    return "source" if usd is not None else "missing"


class SeedRoleError(RuntimeError):
    """Seeding needs the owner role (MIGRATION_DATABASE_URL), never the application's."""


def lineage(version: str) -> str:
    return f"seed:scenarios@{version}"


def require_owner_url(migration_url: str | None, app_url: str | None) -> str:
    """The owner URL, or an error: seeding never falls back to DATABASE_URL."""
    if not migration_url:
        raise SeedRoleError("MIGRATION_DATABASE_URL is not set: seeding needs the owner role")
    if app_url and make_url(migration_url).username == make_url(app_url).username:
        raise SeedRoleError("MIGRATION_DATABASE_URL uses the application's role")
    return migration_url


def require_owner(session: Session) -> None:
    owner, user = session.execute(
        text(
            "SELECT tableowner, current_user FROM pg_tables "
            "WHERE schemaname = current_schema() AND tablename = 'customers'"
        )
    ).one()
    if owner != user:
        raise SeedRoleError(f"{user} does not own the Core Banking tables (owner: {owner})")


@dataclass
class SeedRows:
    customers: list[dict[str, Any]] = field(default_factory=list)
    products: list[dict[str, Any]] = field(default_factory=list)
    transactions: list[dict[str, Any]] = field(default_factory=list)
    cases: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class SeedReport:
    customers: int
    products: int
    transactions: int
    cases: int
    removed: dict[str, int]


def build_rows(
    scenarios: list[Scenario],
    business_date: date,
    hash_key: str,
    version: str,
    config: PolicyConfig | None = None,
    ingested_at: datetime | None = None,
) -> SeedRows:
    config = config or load_policy_config()
    source = lineage(version)
    loaded = ingested_at or datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    as_of = datetime.combine(business_date + timedelta(days=1), time(6, 0))
    rows = SeedRows()
    for s in scenarios:
        stamp = {"source_file": source, "ingested_at": loaded}
        owners = [(s.customer_id, s.document_number, "")]
        if any(t.foreign for t in s.transactions):
            owners.append((s.foreign_customer_id, f"{s.document_number}X", "X"))
        for customer_id, document, _ in owners:
            rows.customers.append(
                {
                    "customer_id": customer_id,
                    "document_type": "CC",
                    "document_hash": document_hash(document, hash_key),
                    "customer_status": s.customer.status
                    if customer_id == s.customer_id
                    else "Active",
                    "country": s.customer.country,
                    "registration_date": datetime(2021, 3, 1, 9),
                    "last_updated": datetime(2026, 1, 15, 9),
                    "gender": s.customer.gender,
                    "age_band": s.customer.age_band,
                    "detected_accent": ACCENTS[s.customer.country],
                    "segment": s.customer.segment,
                    "credit_score": None,
                    "estimated_monthly_income": None,
                    "occupation": None,
                    "marital_status": None,
                    "education_level": None,
                    **stamp,
                }
            )
        foreign_products = {t.product for t in s.transactions if t.foreign}
        for p in s.products:
            for customer_id, _, suffix in owners:
                if suffix and p.key not in foreign_products:
                    continue
                rows.products.append(
                    {
                        "product_id": s.product_id(p.key) + suffix,
                        "customer_id": customer_id,
                        "product_type": p.type,
                        "product_number_last4": p.last4,
                        "currency": p.currency,
                        "product_status": p.status,
                        "opening_date": date(2023, 5, 2),
                        "expiration_date": None,
                        "last_updated": datetime(2026, 1, 15, 9),
                        **stamp,
                    }
                )
        for t in s.transactions:
            suffix = "X" if t.foreign else ""
            moment = transaction_moment(t, business_date)
            usd = t.usd
            rows.transactions.append(
                {
                    "transaction_id": s.transaction_id(t.key),
                    "customer_id": s.foreign_customer_id if t.foreign else s.customer_id,
                    "product_id": s.product_id(t.product) + suffix,
                    "transaction_date": moment,
                    "process_date": moment.date(),
                    "transaction_type": t.type,
                    "transaction_category": None,
                    "amount": t.amount,
                    "currency": t.currency,
                    "amount_usd": usd,
                    "amount_usd_source": _usd_source(t.currency, usd),
                    "fx_rate_date": None,
                    "channel": CHANNELS.get(t.type, "POS"),
                    "merchant_name": t.merchant,
                    "merchant_category": t.category,
                    "transaction_status": t.status,
                    "response_code": "00" if t.status == "Approved" else "05",
                    "fraud_score": None if t.fraud_score is None else Decimal(str(t.fraud_score)),
                    **stamp,
                }
            )
        for index, prior in enumerate(s.prior_cases, start=1):
            txn = s.transaction(prior.transaction)
            tier = tier_of(txn.usd, config)
            disputed = s.transaction_id(prior.transaction)
            rows.cases.append(
                {
                    "case_id": s.case_id(index),
                    "customer_id": s.customer_id,
                    "transaction_id": disputed,
                    "reason_code": prior.reason_code.value,
                    "status": prior.status.value,
                    "tier": tier.value,
                    "amount": txn.amount,
                    "currency": txn.currency,
                    "amount_usd": txn.usd or Decimal(0),
                    "provisional_credit_flag": CREDIT_FLAGS[tier.value],
                    "idempotency_key": f"{disputed}:{prior.reason_code.value}",
                    "business_created_at": as_of - timedelta(days=prior.days_ago),
                }
            )
    return rows


class SeedRemovalError(RuntimeError):
    """--remove would have to touch the audit trail, which is append-only."""


def _clear(session: Session, *, core: bool) -> dict[str, int]:
    """Reset the state of SEED- customers; with ``core`` also delete their rows. ``audit_logs``
    is never read for writing: it is append-only."""
    seed_documents = select(Customer.document_hash).where(Customer.customer_id.like(SEED_LIKE))
    changed: dict[str, int] = {}
    statements: list[tuple[str, Any]] = [
        (
            "otp_challenges",
            delete(OtpChallenge).where(OtpChallenge.document_hash.in_(seed_documents)),
        ),
        ("otp_failures", delete(OtpFailure).where(OtpFailure.document_hash.in_(seed_documents))),
        ("card_blocks", delete(CardBlock).where(CardBlock.customer_id.like(SEED_LIKE))),
        (
            "handoff_packets",
            delete(HandoffPacketRow).where(
                or_(
                    HandoffPacketRow.customer_id.like(SEED_LIKE),
                    HandoffPacketRow.transaction_id.like(SEED_LIKE),
                )
            ),
        ),
        ("cases", delete(Case).where(Case.customer_id.like(SEED_LIKE))),
    ]
    if core:
        seed_sessions = select(SessionRow.session_id).where(SessionRow.customer_id.like(SEED_LIKE))
        traced = session.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.session_id.in_(seed_sessions))
        )
        if traced:
            raise SeedRemovalError(
                f"nothing was removed: {traced} audit records point to sessions of SEED- "
                "customers. The audit trail is append-only, so seeded customers with traces "
                "stay by design. To start from scratch, use a new database."
            )
        statements += [
            ("sessions", delete(SessionRow).where(SessionRow.customer_id.like(SEED_LIKE))),
            ("transactions", delete(Transaction).where(Transaction.transaction_id.like(SEED_LIKE))),
            ("products", delete(Product).where(Product.product_id.like(SEED_LIKE))),
            ("customers", delete(Customer).where(Customer.customer_id.like(SEED_LIKE))),
        ]
    else:  # sessions of earlier runs are revoked, never deleted: their traces stay untouched
        statements.append(
            (
                "sessions_revoked",
                update(SessionRow)
                .where(SessionRow.customer_id.like(SEED_LIKE), SessionRow.revoked_at.is_(None))
                .values(  # never before the session's own times (ck_sessions_revoked_order)
                    revoked_at=func.greatest(
                        func.clock_timestamp(), SessionRow.created_at, SessionRow.last_activity_at
                    )
                ),
            )
        )
    for name, statement in statements:
        changed[name] = session.execute(statement).rowcount or 0  # type: ignore[attr-defined]
    return changed


def _upsert(session: Session, table: Table, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    keys = [column.name for column in table.primary_key.columns]
    statement = insert(table).values(rows)
    updates = {name: statement.excluded[name] for name in rows[0] if name not in keys}
    session.execute(statement.on_conflict_do_update(index_elements=keys, set_=updates))


def seed(session: Session, rows: SeedRows) -> SeedReport:
    """Idempotent: every case back in the state of its specification. The caller commits."""
    for row in rows.customers + rows.products + rows.transactions + rows.cases:
        identifier = (
            row.get("case_id")
            or row.get("transaction_id")
            or row.get("product_id")
            or row["customer_id"]
        )
        if not str(identifier).startswith(SEED_PREFIX):
            raise ValueError(f"not a seeded row: {identifier}")
    removed = _clear(session, core=False)
    _upsert(session, Customer.__table__, rows.customers)  # type: ignore[arg-type]
    _upsert(session, Product.__table__, rows.products)  # type: ignore[arg-type]
    _upsert(session, Transaction.__table__, rows.transactions)  # type: ignore[arg-type]
    _upsert(session, Case.__table__, rows.cases)  # type: ignore[arg-type]
    return SeedReport(
        customers=len(rows.customers),
        products=len(rows.products),
        transactions=len(rows.transactions),
        cases=len(rows.cases),
        removed=removed,
    )


def remove(session: Session) -> dict[str, int]:
    """Delete every SEED- row and the state left on SEED- customers. The caller commits."""
    return _clear(session, core=True)


def count_seeded(session: Session) -> dict[str, int]:
    return {
        "customers": session.scalar(
            select(func.count()).where(Customer.customer_id.like(SEED_LIKE))
        )
        or 0,
        "products": session.scalar(select(func.count()).where(Product.product_id.like(SEED_LIKE)))
        or 0,
        "transactions": session.scalar(
            select(func.count()).where(Transaction.transaction_id.like(SEED_LIKE))
        )
        or 0,
        "cases": session.scalar(select(func.count()).where(Case.customer_id.like(SEED_LIKE))) or 0,
    }
