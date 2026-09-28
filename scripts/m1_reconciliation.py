"""M1 reconciliation: raw rows -> rejected rows by reason -> loaded rows, plus data checks.

Counts raw rows independently of DuckDB (CSV records read with the csv module), takes the
deduplication figures from reports/quality_<table>.json, and queries the loaded database.
Writes reports/m1_reconciliation.json with aggregate figures only (no identifiers).

    python scripts/m1_reconciliation.py
"""

from __future__ import annotations

import csv
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import psycopg  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.settings import get_settings  # noqa: E402

RAW = ROOT / "data" / "raw"
REPORTS = ROOT / "reports"
TABLES = ("customers", "products", "transactions")


def raw_files(table: str) -> list[Path]:
    partitioned = sorted((RAW / table).rglob("*.csv")) if (RAW / table).is_dir() else []
    return partitioned or [RAW / f"{table}.csv"]


def count_records(files: list[Path]) -> int:
    total = 0
    for path in files:
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.reader(handle)
            next(reader, None)  # header
            total += sum(1 for _ in reader)
    return total


def rows(cur: psycopg.Cursor[Any], query: str) -> list[tuple[Any, ...]]:
    cur.execute(query)  # fixed queries, no user input
    return cur.fetchall()


def null_breakdown(cur: psycopg.Cursor[Any], column: str) -> dict[str, object]:
    result: dict[str, object] = {}
    for group, total, nulls in rows(
        cur,
        f"SELECT {column}::text, count(*), count(*) FILTER (WHERE fraud_score IS NULL) "
        f"FROM transactions GROUP BY 1 ORDER BY 1",
    ):
        result[str(group)] = {
            "rows": total,
            "fraud_score_null": nulls,
            "null_rate": round(int(nulls) / int(total), 4),
        }
    return result


def main() -> None:
    settings = get_settings()
    if not settings.database_url:
        sys.exit("DATABASE_URL is not set")
    dsn = make_url(settings.database_url).set(drivername="postgresql")

    reconciliation: dict[str, Any] = {}
    with psycopg.connect(dsn.render_as_string(hide_password=False)) as conn, conn.cursor() as cur:
        for table in TABLES:
            quality = json.loads((REPORTS / f"quality_{table}.json").read_text(encoding="utf-8"))
            files = raw_files(table)
            raw_rows = count_records(files)
            loaded = rows(cur, f"SELECT count(*) FROM {table}")[0][0]
            rejected = {
                "null_primary_key": quality["pk_nulls"],
                "duplicate_primary_key": quality["pk_duplicate_rows"],
                # The data contract rejects the whole load instead of single rows.
                "data_contract": 0,
            }
            reconciliation[table] = {
                "raw_files": len(files),
                "raw_rows": raw_rows,
                "raw_rows_per_ingest_report": quality["raw_rows"],
                "rejected": rejected,
                "silver_rows": quality["silver_rows"],
                "loaded_rows": loaded,
                "balanced": raw_rows - sum(rejected.values()) == loaded == quality["silver_rows"],
            }

        fx: dict[Any, Any] = dict(
            rows(
                cur,
                "SELECT (CAST(transaction_date AS date) - fx_rate_date)::text, count(*) "
                "FROM transactions WHERE amount_usd_source = 'fx_rate' GROUP BY 1 ORDER BY 1",
            )
        )
        adjustments = rows(
            cur,
            "SELECT count(*), count(*) FILTER (WHERE amount < 0), count(*) FILTER (WHERE amount > 0),"
            " count(merchant_name), count(transaction_category), count(merchant_category),"
            " min(amount_usd), percentile_cont(0.5) WITHIN GROUP (ORDER BY amount_usd),"
            " max(amount_usd) FROM transactions WHERE transaction_type = 'Adjustment'",
        )[0]
        adjustment_products: dict[Any, Any] = dict(
            rows(
                cur,
                "SELECT p.product_type, count(*) FROM transactions t JOIN products p "
                "USING (product_id) WHERE t.transaction_type = 'Adjustment' GROUP BY 1 ORDER BY 1",
            )
        )
        adjustment_channels: dict[Any, Any] = dict(
            rows(
                cur,
                "SELECT channel, count(*) FROM transactions WHERE transaction_type = 'Adjustment' "
                "GROUP BY 1 ORDER BY 1",
            )
        )
        total, nulls = rows(
            cur, "SELECT count(*), count(*) FILTER (WHERE fraud_score IS NULL) FROM transactions"
        )[0]

    report: dict[str, Any] = {
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "business_date": settings.business_date.isoformat(),
        "reconciliation": reconciliation,
        "fraud_score_nulls": {
            "total_rows": total,
            "null_rows": nulls,
            "by_transaction_type": None,
            "by_channel": None,
            "by_is_fraud": None,
        },
        "fx_rate_age_days": {str(k): v for k, v in fx.items()},
        "adjustments": {
            "rows": adjustments[0],
            "negative_amount": adjustments[1],
            "positive_amount": adjustments[2],
            "with_merchant_name": adjustments[3],
            "with_transaction_category": adjustments[4],
            "with_merchant_category": adjustments[5],
            "amount_usd_min": str(adjustments[6]),
            "amount_usd_median": str(adjustments[7]),
            "amount_usd_max": str(adjustments[8]),
            "by_product_type": adjustment_products,
            "by_channel": adjustment_channels,
        },
    }
    with psycopg.connect(dsn.render_as_string(hide_password=False)) as conn, conn.cursor() as cur:
        breakdown = report["fraud_score_nulls"]
        assert isinstance(breakdown, dict)
        breakdown["by_transaction_type"] = null_breakdown(cur, "transaction_type")
        breakdown["by_channel"] = null_breakdown(cur, "channel")
        breakdown["by_is_fraud"] = null_breakdown(cur, "is_fraud")

    out = REPORTS / "m1_reconciliation.json"
    out.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
