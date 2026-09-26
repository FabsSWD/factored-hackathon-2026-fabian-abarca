"""
Raw (CSV) -> silver (Parquet) ingestion with data quality reports.
Factored AI & Data Hackathon 2026

Usage:
    pip install duckdb
    python ingest.py                         # every downloaded table
    python ingest.py complaints customers    # selected tables only

Inputs:   data/raw/<tabla>/year=YYYY/month=MM/day=DD/*.csv   (fact tables)
           data/raw/<tabla>.csv                              (dimension tables)
Outputs:   data/silver/<tabla>.parquet
           reports/quality_<tabla>.json
"""
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb

# Look for data/raw from the current folder, if run from inside "data/", use its parent
_BASE = Path.cwd()
if not (_BASE / "data" / "raw").is_dir() and (_BASE / "raw").is_dir():
    _BASE = _BASE.parent
RAW = _BASE / "data" / "raw"
SILVER = _BASE / "data" / "silver"
REPORTS = _BASE / "reports"

# Primary key of each table (from data dictionary)
PRIMARY_KEYS = {
    "customers": "customer_id",
    "products": "product_id",
    "branches": "branch_id",
    "service_agents": "agent_id",
    "marketing_campaigns": "campaign_id",
    "transactions": "transaction_id",
    "call_center_interactions": "interaction_id",
    "call_transcripts": "transcript_id",
    "satisfaction_surveys": "survey_id",
    "digital_events": "event_id",
    "complaints": "complaint_id",
    "campaign_sends": "send_id",
}

LINEAGE_COLS = {"_source_file", "year", "month", "day"}


def source_files(table: str) -> list[Path]:
    folder = RAW / table
    if folder.is_dir():
        return sorted(folder.rglob("*.csv"))
    single = RAW / f"{table}.csv"
    return [single] if single.exists() else []


def header_variants(files: list[Path]) -> list[dict]:
    variants: dict[tuple, list[str]] = {}
    for f in files:
        with open(f, encoding="utf-8-sig", newline="") as fh:
            header = tuple(next(csv.reader(fh), []))
        variants.setdefault(header, []).append(f.relative_to(RAW).as_posix())
    return [
        {
            "columns": list(h),
            "file_count": len(fs),
            "first_file": fs[0],
            "last_file": fs[-1],
        }
        for h, fs in variants.items()
    ]


def ingest(con: duckdb.DuckDBPyConnection, table: str) -> dict:
    files = source_files(table)
    if not files:
        print(f"[skip] {table}: no files in {RAW}")
        return {}
    pk = PRIMARY_KEYS[table]
    file_list = [f.as_posix() for f in files]

    print(f"[read] {table}: {len(files)} file(s)")
    con.execute(
        """
        CREATE OR REPLACE TABLE raw AS
        SELECT * EXCLUDE (filename), filename AS _source_file
        FROM read_csv(?, union_by_name = true, hive_partitioning = true,
                      filename = true, header = true)
        """,
        [file_list],
    )

    cols = [r[0] for r in con.execute("DESCRIBE raw").fetchall()]
    data_cols = [c for c in cols if c not in LINEAGE_COLS]
    quoted = ", ".join(f'"{c}"' for c in data_cols)

    total = con.execute("SELECT count(*) FROM raw").fetchone()[0]
    distinct_rows = con.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT {quoted} FROM raw)"
    ).fetchone()[0]
    pk_nulls, pk_distinct = con.execute(
        f'SELECT count(*) - count("{pk}"), count(DISTINCT "{pk}") FROM raw'
    ).fetchone()

    null_exprs = ", ".join(f'count(*) - count("{c}")' for c in data_cols)
    null_counts = con.execute(f"SELECT {null_exprs} FROM raw").fetchone()
    null_rates = {
        c: round(n / total, 4) for c, n in zip(data_cols, null_counts) if n
    }

    # One row per primary key, deduplicates stuff
    order = [c for c in ("last_updated", "process_date") if c in cols]
    order_sql = ", ".join(f'"{c}" DESC NULLS LAST' for c in order + ["_source_file"])

    SILVER.mkdir(parents=True, exist_ok=True)
    out = (SILVER / f"{table}.parquet").as_posix()
    ingested_at = datetime.now(timezone.utc).isoformat()
    con.execute(
        f"""
        COPY (
            SELECT * EXCLUDE (_rn), TIMESTAMP '{ingested_at[:19]}' AS _ingested_at
            FROM (
                SELECT *, row_number() OVER (PARTITION BY "{pk}" ORDER BY {order_sql}) AS _rn
                FROM raw
                WHERE "{pk}" IS NOT NULL
            )
            WHERE _rn = 1
        ) TO '{out}' (FORMAT parquet, COMPRESSION zstd)
        """
    )
    silver_rows = con.execute(f"SELECT count(*) FROM '{out}'").fetchone()[0]

    report = {
        "table": table,
        "ingested_at": ingested_at,
        "source_files": len(files),
        "raw_rows": total,
        "exact_duplicate_rows": total - distinct_rows,
        "pk_column": pk,
        "pk_nulls": pk_nulls,
        "pk_duplicate_rows": total - pk_nulls - pk_distinct,
        "silver_rows": silver_rows,
        "dropped_rows": total - silver_rows,
        "dedup_order": order + ["_source_file"],
        "null_rates": null_rates,
        "schema_variants": header_variants(files),
        "column_types": {
            r[0]: r[1] for r in con.execute("DESCRIBE raw").fetchall()
        },
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"quality_{table}.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8"
    )

    print(
        f"[done] {table}: raw={total:,} silver={silver_rows:,} "
        f"exact_dups={report['exact_duplicate_rows']:,} "
        f"pk_dups={report['pk_duplicate_rows']:,} "
        f"schema_variants={len(report['schema_variants'])}"
    )
    return report


def main() -> None:
    tables = sys.argv[1:] or [
        t for t in PRIMARY_KEYS if source_files(t)
    ]
    unknown = [t for t in tables if t not in PRIMARY_KEYS]
    if unknown:
        sys.exit(f"Unknown tables: {unknown}")
    if not tables:
        sys.exit(
            f"No data found in {RAW}. Run the script from the folder that "
            f"contains data/raw (or from data/)."
        )
    print(f"[info] reading from {RAW}")

    con = duckdb.connect()
    con.execute("PRAGMA enable_progress_bar")
    for t in tables:
        ingest(con, t)


if __name__ == "__main__":
    main()
