"""Find one customer per manual-test scenario, with what to type in the chat.

    python scripts/find_test_customers.py

Local use only, read-only: it reads the database with DATABASE_URL (the application role) and
the document number from data/raw/customers.csv by customer_id, because the database keeps
only its HMAC. The document number is printed on screen to log in with scripts/manual_chat.py
and is never written to a file.

Scenarios (inside the filing window, active customer and card unless stated):
- t1_purchase      an approved card purchase in tier T1, no case yet, low fraud score;
- pending          a pending transaction;
- duplicate_pair   two approved charges, same product, merchant, amount and currency, within
                   DUPLICATE_WINDOW_HOURS;
- same_day         several approved card purchases on the same day (the candidate list);
- high_fraud       an approved card purchase with fraud_score above FRAUD_SCORE_ESCALATE,
                   below the T3 amount.
"""

from __future__ import annotations

import csv
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.config import load_policy_config  # noqa: E402
from app.settings import get_settings  # noqa: E402
from app.storage.data_contract import CARD_PRODUCT_TYPES  # noqa: E402

RAW_CUSTOMERS = ROOT / "data" / "raw" / "customers.csv"

NOT_FOUND = {
    "duplicate_pair": "the data has no pair that meets RC_DUPLICATE; the duplicate flow can"
    " only be seen with fakes (python scripts/demo_conversation.py)",
}

RECENT_CARD_PURCHASES = """
    SELECT t.customer_id, t.transaction_id, t.transaction_date, t.merchant_name, t.amount,
           t.currency, t.amount_usd, t.fraud_score, t.product_id, t.transaction_status
    FROM transactions t
    JOIN products p ON p.product_id = t.product_id
    JOIN customers c ON c.customer_id = t.customer_id
    WHERE t.transaction_type = 'Purchase'
      AND p.product_type = ANY(%(cards)s) AND p.product_status = 'Active'
      AND c.customer_status = 'Active'
      AND t.transaction_date >= %(start)s AND t.transaction_date < %(as_of)s
      AND NOT EXISTS (SELECT 1 FROM cases k WHERE k.transaction_id = t.transaction_id)
"""

QUERIES: dict[str, str] = {
    "t1_purchase": f"""
        SELECT * FROM ({RECENT_CARD_PURCHASES}) r
        WHERE transaction_status = 'Approved' AND amount_usd <= %(t1_max)s
          AND (fraud_score IS NULL OR fraud_score < %(fraud)s)
        ORDER BY transaction_date DESC LIMIT 1
    """,
    "pending": f"""
        SELECT * FROM ({RECENT_CARD_PURCHASES}) r
        WHERE transaction_status = 'Pending'
        ORDER BY transaction_date DESC LIMIT 1
    """,
    "duplicate_pair": f"""
        WITH r AS ({RECENT_CARD_PURCHASES})
        SELECT b.*, a.transaction_id AS twin_id, a.transaction_date AS twin_date
        FROM r a JOIN r b
          ON a.customer_id = b.customer_id AND a.product_id = b.product_id
         AND a.merchant_name = b.merchant_name AND a.amount = b.amount
         AND a.currency = b.currency AND a.transaction_id <> b.transaction_id
         AND b.transaction_date > a.transaction_date
         AND b.transaction_date - a.transaction_date <= %(dup_window)s
        WHERE a.transaction_status = 'Approved' AND b.transaction_status = 'Approved'
          AND b.amount_usd <= %(t1_max)s
          AND (b.fraud_score IS NULL OR b.fraud_score < %(fraud)s)
        ORDER BY b.transaction_date DESC LIMIT 1
    """,
    "same_day": f"""
        WITH r AS ({RECENT_CARD_PURCHASES}),
        day AS (
            SELECT customer_id, CAST(transaction_date AS DATE) AS d
            FROM r WHERE transaction_status = 'Approved' AND amount_usd <= %(t1_max)s
            GROUP BY customer_id, CAST(transaction_date AS DATE)
            HAVING COUNT(*) BETWEEN 2 AND 3
            ORDER BY d DESC LIMIT 1
        )
        SELECT r.* FROM r JOIN day ON r.customer_id = day.customer_id
         AND CAST(r.transaction_date AS DATE) = day.d
        WHERE r.transaction_status = 'Approved'
        ORDER BY r.transaction_date
    """,
    "high_fraud": f"""
        SELECT * FROM ({RECENT_CARD_PURCHASES}) r
        WHERE transaction_status = 'Approved' AND fraud_score > %(fraud)s
          AND amount_usd <= %(t3_min)s
        ORDER BY transaction_date DESC LIMIT 1
    """,
}


def documents(customer_ids: set[str]) -> dict[str, str]:
    """Document numbers of these customers, read from the raw file (never stored)."""
    found: dict[str, str] = {}
    if not RAW_CUSTOMERS.exists():
        return found
    with RAW_CUSTOMERS.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["customer_id"] in customer_ids:
                found[row["customer_id"]] = row["document_number"]
                if len(found) == len(customer_ids):
                    break
    return found


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # merchant names on a Windows console
    settings = get_settings()
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    parameters = load_policy_config().parameters
    as_of = settings.as_of
    values: dict[str, Any] = {
        "cards": sorted(CARD_PRODUCT_TYPES),
        # Business days within DISPUTE_WINDOW_DAYS of BUSINESS_DATE (policy §15).
        "start": as_of - timedelta(days=parameters.DISPUTE_WINDOW_DAYS + 1),
        "as_of": as_of,
        "t1_max": parameters.PROVISIONAL_CREDIT_AUTO_MAX_USD,
        "t3_min": parameters.AUTO_INTAKE_MAX_USD,
        "fraud": parameters.FRAUD_SCORE_ESCALATE,
        "dup_window": timedelta(hours=parameters.DUPLICATE_WINDOW_HOURS),
    }
    dsn = make_url(settings.database_url).set(drivername="postgresql")
    results: dict[str, list[dict[str, Any]]] = {}
    with psycopg.connect(dsn.render_as_string(hide_password=False), row_factory=dict_row) as conn:
        conn.read_only = True
        for name, query in QUERIES.items():
            results[name] = conn.execute(query, values).fetchall()

    docs = documents({row["customer_id"] for rows in results.values() for row in rows})
    print(f"Business date {settings.business_date} (as_of {as_of}); window from {values['start']}")
    for name, rows in results.items():
        print(f"\n=== {name} ===")
        if not rows:
            print(f"  (no customer found{': ' + NOT_FOUND[name] if name in NOT_FOUND else ''})")
            continue
        customer = rows[0]["customer_id"]
        print(f"  customer_id: {customer}   document: {docs.get(customer, '(not in raw file)')}")
        for row in rows:
            twin = (
                f"   twin {row['twin_id']} at {row['twin_date']:%Y-%m-%d %H:%M}"
                if row.get("twin_id")
                else ""
            )
            score = f"   fraud_score {row['fraud_score']}" if name == "high_fraud" else ""
            merchant = row["merchant_name"] or "(no merchant)"
            print(
                f"  {row['transaction_date']:%Y-%m-%d %H:%M}  {merchant}"
                f"  {row['currency']} {row['amount']:,.2f}  ({row['transaction_status']})"
                f"  {row['transaction_id']}{twin}{score}"
            )


if __name__ == "__main__":
    main()
