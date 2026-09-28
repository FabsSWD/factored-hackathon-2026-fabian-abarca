"""Minimal synthetic Core Banking dataset, written as raw CSV with the real headers.

It goes through the same pipeline as the supplied data (scripts/ingest.py -> core -> loader),
so tests exercise lineage, deduplication across partitions and the USD conversion as well.

Scenarios (business date 2026-06-17, cutoff 06:00, FX_MAX_STALENESS_DAYS 3):

- TRX-T1-PURCHASE: approved card purchase in USD, 50.00 (T1, amount_usd from identity).
  Reposted in the next partition with a stale copy: deduplicated, and a late arrival.
- TRX-T3-PURCHASE: approved card purchase in USD, 1500.00 (T3).
- TRX-PENDING, TRX-DECLINED, TRX-REVERSED: GATE-06 statuses.
- TRX-DUP-FIRST, TRX-DUP-SECOND: same merchant, amount and product 20 hours apart
  (RC_DUPLICATE).
- TRX-COP-SOURCE: transfer in COP with amount_usd supplied (source).
- TRX-COP-FX: payment in COP without amount_usd, same-day rate (fx_rate).
- TRX-ARS-STALE: ARS without amount_usd, latest rate 2 days old (fx_rate).
- TRX-ARS-MISSING: ARS without amount_usd, latest rate 5 days old (missing, T3).
- TRX-FEE: Adjustment on a personal loan (RC_FEE).
- TRX-NEXT-DAY: 03:00 on 2026-06-18, in the 2026-06-17 partition (business-day cutoff).
- TRX-HIGH-FRAUD, TRX-NO-SCORE: fraud_score 91.5 (ESC-04) and null.
- TRX-OTHER-CUSTOMER, TRX-SUSPENDED: other customers (GATE-04, GATE-03).
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

CUSTOMER = "CLI-ALPHA0000001"
OTHER_CUSTOMER = "CLI-BETA00000002"
SUSPENDED_CUSTOMER = "CLI-GAMMA0000003"

CARD = "PRD-CARDALPHA001"
ACCOUNT = "PRD-ACCTALPHA001"
LOAN = "PRD-LOANALPHA001"
ARS_ACCOUNT = "PRD-ARSALPHA0001"
OTHER_CARD = "PRD-CARDBETA0001"
SUSPENDED_CARD = "PRD-CARDGAMMA001"

BUSINESS_DATE = date(2026, 6, 17)

CUSTOMER_HEADER = [
    "customer_id",
    "document_number",
    "document_type",
    "first_name",
    "last_name",
    "date_of_birth",
    "gender",
    "email",
    "mobile_phone",
    "landline_phone",
    "address",
    "city",
    "state",
    "country",
    "postal_code",
    "detected_accent",
    "segment",
    "credit_score",
    "estimated_monthly_income",
    "occupation",
    "marital_status",
    "education_level",
    "registration_date",
    "registration_branch_id",
    "customer_status",
    "last_updated",
    "accepts_marketing",
]
PRODUCT_HEADER = [
    "product_id",
    "customer_id",
    "product_type",
    "product_number",
    "currency",
    "current_balance",
    "credit_limit",
    "interest_rate",
    "opening_date",
    "expiration_date",
    "opening_branch_id",
    "product_status",
    "opening_channel",
    "has_linked_app",
    "days_past_due",
    "last_transaction_date",
    "last_updated",
]
TRANSACTION_HEADER = [
    "transaction_id",
    "transaction_date",
    "process_date",
    "product_id",
    "customer_id",
    "transaction_type",
    "transaction_category",
    "amount",
    "currency",
    "amount_usd",
    "channel",
    "branch_id",
    "merchant_name",
    "merchant_category",
    "transaction_country",
    "transaction_city",
    "transaction_status",
    "response_code",
    "is_fraud",
    "fraud_score",
    "latitude",
    "longitude",
]
RATE_HEADER = [
    "date",
    "source_currency",
    "target_currency",
    "exchange_rate",
    "buy_rate",
    "sell_rate",
    "source",
]

# Personal data columns exist in the raw files so the tests can prove they are never stored.
CUSTOMERS: list[dict[str, str]] = [
    {
        "customer_id": CUSTOMER,
        "document_number": "X1234567",
        "document_type": "Pasaporte",
        "first_name": "Ana",
        "last_name": "Prueba Sintética",
        "date_of_birth": "1990-06-18",  # turns 36 one day after the business date: 35
        "gender": "F",
        "email": "ana@example.test",
        "mobile_phone": "+57 300 000 0000",
        "landline_phone": "",
        "address": "Calle Falsa 123",
        "city": "Bogotá",
        "state": "Cundinamarca",
        "country": "Colombia",
        "postal_code": "110111",
        "detected_accent": "colombian",
        "segment": "Plus",
        "credit_score": "701.0",
        "estimated_monthly_income": "5000000.00",
        "occupation": "Engineer",
        "marital_status": "Single",
        "education_level": "University",
        "registration_date": "2021-07-30 05:39:39",
        "registration_branch_id": "SUC-TEST0001",
        "customer_status": "Active",
        "last_updated": "2021-08-15 05:39:39",
        "accepts_marketing": "False",
    },
    {
        "customer_id": OTHER_CUSTOMER,
        "document_number": "42388496",
        "document_type": "DNI",
        "first_name": "Bruno",
        "last_name": "Otro Cliente",
        "date_of_birth": "1950-01-01",
        "gender": "M",
        "email": "bruno@example.test",
        "mobile_phone": "",
        "landline_phone": "",
        "address": "Av. Siempre Viva 742",
        "city": "Mendoza",
        "state": "Mendoza",
        "country": "Argentina",
        "postal_code": "",
        "detected_accent": "argentinian",
        "segment": "Basic",
        "credit_score": "576.0",
        "estimated_monthly_income": "",
        "occupation": "Retired",
        "marital_status": "Married",
        "education_level": "",
        "registration_date": "2022-12-09 13:10:47",
        "registration_branch_id": "SUC-TEST0002",
        "customer_status": "Active",
        "last_updated": "2023-05-15 13:10:47",
        "accepts_marketing": "True",
    },
    {
        "customer_id": SUSPENDED_CUSTOMER,
        "document_number": "C9988776",
        "document_type": "CC",
        "first_name": "Carla",
        "last_name": "Suspendida",
        "date_of_birth": "2003-01-01",
        "gender": "F",
        "email": "carla@example.test",
        "mobile_phone": "",
        "landline_phone": "",
        "address": "",
        "city": "Guadalajara",
        "state": "Jalisco",
        "country": "México",
        "postal_code": "44100",
        "detected_accent": "mexican",
        "segment": "Basic",
        "credit_score": "",
        "estimated_monthly_income": "",
        "occupation": "",
        "marital_status": "",
        "education_level": "",
        "registration_date": "2024-01-01 00:00:00",
        "registration_branch_id": "SUC-TEST0003",
        "customer_status": "Suspended",
        "last_updated": "2024-01-01 00:00:00",
        "accepts_marketing": "False",
    },
]


def _product(
    product_id: str, customer_id: str, product_type: str, number: str, currency: str, status: str
) -> dict[str, str]:
    return {
        "product_id": product_id,
        "customer_id": customer_id,
        "product_type": product_type,
        "product_number": number,
        "currency": currency,
        "current_balance": "1000.00",
        "credit_limit": "",
        "interest_rate": "0.0",
        "opening_date": "2024-01-01",
        "expiration_date": "",
        "opening_branch_id": "SUC-TEST0001",
        "product_status": status,
        "opening_channel": "App",
        "has_linked_app": "True",
        "days_past_due": "",
        "last_transaction_date": "2026-06-17 10:00:00",
        "last_updated": "2026-06-17 10:00:00",
    }


PRODUCTS: list[dict[str, str]] = [
    _product(CARD, CUSTOMER, "Tarjeta Crédito", "4111111111114821", "USD", "Active"),
    _product(ACCOUNT, CUSTOMER, "Cuenta Corriente", "4332181960", "COP", "Active"),
    _product(LOAN, CUSTOMER, "Préstamo Personal", "7700112233", "COP", "Active"),
    _product(ARS_ACCOUNT, CUSTOMER, "Cuenta Ahorro", "5500123499", "ARS", "Active"),
    _product(OTHER_CARD, OTHER_CUSTOMER, "Tarjeta Débito", "4959310341316475", "USD", "Blocked"),
    _product(
        SUSPENDED_CARD, SUSPENDED_CUSTOMER, "Tarjeta Débito", "4000000000000002", "USD", "Active"
    ),
]


@dataclass(frozen=True)
class Tx:
    transaction_id: str
    at: str  # naive local timestamp
    process_date: str
    product_id: str
    customer_id: str
    transaction_type: str
    amount: str
    currency: str
    status: str = "Approved"
    amount_usd: str = ""
    merchant: str = ""
    fraud_score: str = "12.50"
    channel: str = "POS"

    def row(self) -> dict[str, str]:
        return {
            "transaction_id": self.transaction_id,
            "transaction_date": self.at,
            "process_date": self.process_date,
            "product_id": self.product_id,
            "customer_id": self.customer_id,
            "transaction_type": self.transaction_type,
            "transaction_category": "Food" if self.merchant else "",
            "amount": self.amount,
            "currency": self.currency,
            "amount_usd": self.amount_usd,
            "channel": self.channel,
            "branch_id": "",
            "merchant_name": self.merchant,
            "merchant_category": "Restaurants" if self.merchant else "",
            "transaction_country": "Colombia",
            "transaction_city": "Bogotá",
            "transaction_status": self.status,
            "response_code": "00" if self.status == "Approved" else "05",
            "is_fraud": "False",
            "fraud_score": self.fraud_score,
            "latitude": "",
            "longitude": "",
        }


TRANSACTIONS: list[Tx] = [
    Tx(
        "TRX-T1-PURCHASE",
        "2026-06-16 10:00:00",
        "2026-06-16",
        CARD,
        CUSTOMER,
        "Purchase",
        "50.00",
        "USD",
        merchant="Cafe Sintetico",
    ),
    Tx(
        "TRX-T3-PURCHASE",
        "2026-06-15 12:30:00",
        "2026-06-15",
        CARD,
        CUSTOMER,
        "Purchase",
        "1500.00",
        "USD",
        merchant="Electro Mundo",
    ),
    Tx(
        "TRX-PENDING",
        "2026-06-17 09:00:00",
        "2026-06-17",
        CARD,
        CUSTOMER,
        "Purchase",
        "20.00",
        "USD",
        status="Pending",
        merchant="Kiosko 24",
    ),
    Tx(
        "TRX-DECLINED",
        "2026-06-17 09:05:00",
        "2026-06-17",
        CARD,
        CUSTOMER,
        "Purchase",
        "20.00",
        "USD",
        status="Declined",
        merchant="Kiosko 24",
    ),
    Tx(
        "TRX-REVERSED",
        "2026-06-14 18:00:00",
        "2026-06-14",
        CARD,
        CUSTOMER,
        "Purchase",
        "35.00",
        "USD",
        status="Reversed",
        merchant="Libreria Uno",
    ),
    Tx(
        "TRX-DUP-FIRST",
        "2026-06-15 08:00:00",
        "2026-06-15",
        CARD,
        CUSTOMER,
        "Purchase",
        "18.90",
        "USD",
        merchant="Streaming Plus",
    ),
    Tx(
        "TRX-DUP-SECOND",
        "2026-06-16 04:00:00",
        "2026-06-15",
        CARD,
        CUSTOMER,
        "Purchase",
        "18.90",
        "USD",
        merchant="Streaming Plus",
    ),
    Tx(
        "TRX-COP-SOURCE",
        "2026-06-16 11:00:00",
        "2026-06-16",
        ACCOUNT,
        CUSTOMER,
        "Transfer",
        "400000.00",
        "COP",
        amount_usd="100.00",
        channel="App",
    ),
    Tx(
        "TRX-COP-FX",
        "2026-06-17 15:00:00",
        "2026-06-17",
        ACCOUNT,
        CUSTOMER,
        "Payment",
        "200000.00",
        "COP",
        channel="Web",
    ),
    Tx(
        "TRX-ARS-STALE",
        "2026-06-14 10:00:00",
        "2026-06-14",
        ARS_ACCOUNT,
        CUSTOMER,
        "Withdrawal",
        "30000.00",
        "ARS",
        channel="ATM",
    ),
    Tx(
        "TRX-ARS-MISSING",
        "2026-06-17 10:00:00",
        "2026-06-17",
        ARS_ACCOUNT,
        CUSTOMER,
        "Withdrawal",
        "40000.00",
        "ARS",
        channel="ATM",
    ),
    Tx(
        "TRX-FEE",
        "2026-06-16 07:00:00",
        "2026-06-16",
        LOAN,
        CUSTOMER,
        "Adjustment",
        "120000.00",
        "COP",
        amount_usd="30.00",
        channel="Branch",
    ),
    Tx(
        "TRX-NEXT-DAY",
        "2026-06-18 03:00:00",
        "2026-06-17",
        CARD,
        CUSTOMER,
        "Purchase",
        "12.00",
        "USD",
        merchant="Farmacia Noche",
    ),
    Tx(
        "TRX-HIGH-FRAUD",
        "2026-06-16 22:00:00",
        "2026-06-16",
        CARD,
        CUSTOMER,
        "Purchase",
        "75.00",
        "USD",
        merchant="Tienda Remota",
        fraud_score="91.50",
    ),
    Tx(
        "TRX-NO-SCORE",
        "2026-06-16 13:00:00",
        "2026-06-16",
        CARD,
        CUSTOMER,
        "Purchase",
        "8.00",
        "USD",
        merchant="Cafe Sintetico",
        fraud_score="",
    ),
    Tx(
        "TRX-OTHER-CUSTOMER",
        "2026-06-16 10:00:00",
        "2026-06-16",
        OTHER_CARD,
        OTHER_CUSTOMER,
        "Purchase",
        "50.00",
        "USD",
        merchant="Cafe Sintetico",
    ),
    Tx(
        "TRX-SUSPENDED",
        "2026-06-16 10:00:00",
        "2026-06-16",
        SUSPENDED_CARD,
        SUSPENDED_CUSTOMER,
        "Purchase",
        "10.00",
        "USD",
        merchant="Cafe Sintetico",
    ),
]

# Reposted in a later partition with an older amount: ingest keeps the latest process_date.
REPOSTED = Tx(
    "TRX-T1-PURCHASE",
    "2026-06-16 10:00:00",
    "2026-06-17",
    CARD,
    CUSTOMER,
    "Purchase",
    "50.00",
    "USD",
    merchant="Cafe Sintetico",
)
STALE_COPY = Tx(
    "TRX-T1-PURCHASE",
    "2026-06-16 10:00:00",
    "2026-06-16",
    CARD,
    CUSTOMER,
    "Purchase",
    "49.00",
    "USD",
    merchant="Cafe Sintetico",
)

COP_RATE = "0.00025"
ARS_RATE = "0.001"
ARS_LAST_RATE = date(2026, 6, 12)


def _rates() -> list[dict[str, str]]:
    rows = []
    day = date(2026, 6, 1)
    while day <= BUSINESS_DATE:
        rows.append(_rate(day, "COP", COP_RATE))
        if day <= ARS_LAST_RATE:
            rows.append(_rate(day, "ARS", ARS_RATE))
        rows.append(_rate(day, "COP", "3900.0", target="ARS"))  # not a USD rate: ignored
        day += timedelta(days=1)
    return rows


def _rate(day: date, source: str, rate: str, target: str = "USD") -> dict[str, str]:
    return {
        "date": day.isoformat(),
        "source_currency": source,
        "target_currency": target,
        "exchange_rate": rate,
        "buy_rate": rate,
        "sell_rate": rate,
        "source": "Synthetic",
    }


def _write(path: Path, header: list[str], rows: list[dict[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=header)
        writer.writeheader()
        writer.writerows(rows)


def write_raw_dataset(
    root: Path,
    *,
    transactions: list[Tx] | None = None,
    products: list[dict[str, str]] | None = None,
    customers: list[dict[str, str]] | None = None,
) -> Path:
    """Write ``root/data/raw`` like the supplied dataset. Returns the raw directory."""
    raw = root / "data" / "raw"
    (raw / "customers").mkdir(parents=True, exist_ok=True)  # empty folder, as in the real data
    _write(raw / "customers.csv", CUSTOMER_HEADER, customers or CUSTOMERS)
    _write(raw / "products.csv", PRODUCT_HEADER, products or PRODUCTS)
    _write(raw / "daily_exchange_rates.csv", RATE_HEADER, _rates())
    partitions: dict[str, list[dict[str, str]]] = {}
    rows = [*(transactions or TRANSACTIONS)]
    if transactions is None:
        rows = [tx for tx in rows if tx.transaction_id != STALE_COPY.transaction_id]
        rows += [STALE_COPY, REPOSTED]
    for tx in rows:
        partitions.setdefault(tx.process_date, []).append(tx.row())
    for day, day_rows in partitions.items():
        d = datetime.strptime(day, "%Y-%m-%d")
        folder = raw / "transactions" / f"year={d:%Y}" / f"month={d:%m}" / f"day={d:%d}"
        _write(folder / f"transactions_{d:%Y%m%d}.csv", TRANSACTION_HEADER, day_rows)
    return raw
