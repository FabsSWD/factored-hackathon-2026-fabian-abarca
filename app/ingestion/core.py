"""Silver Parquet -> core Parquet: the rows that go into Core Banking.

Input is the output of ``scripts/ingest.py`` (one row per primary key, deduplicated across
partitions, with ``_source_file`` lineage) plus ``daily_exchange_rates.csv``. This step:

- validates the data contract (``app.storage.data_contract``) and fails with every violation;
- keeps only the columns Core Banking stores (minimization, dispute policy §12);
- establishes the USD equivalent of every transaction (policy §6, glossary);
- writes one Parquet file per table, with columns in the order of the database tables, and a
  report with the figures the policy quotes.
"""

from __future__ import annotations

import hashlib
import hmac
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

import duckdb
from duckdb.func import FunctionNullHandling

from app.settings import business_as_of
from app.storage.data_contract import (
    CURRENCIES,
    CUSTOMER_STATUSES,
    PRODUCT_STATUSES,
    PRODUCT_TYPES,
    TRANSACTION_STATUSES,
    TRANSACTION_TYPES,
    AmountUsdSource,
    age_band,
)
from app.storage.models import CORE_BANKING_TABLES

SAMPLE_SIZE = 5


class DataContractError(ValueError):
    """The supplied data violates the data contract. The message lists every violation."""

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__("Data contract violated:\n- " + "\n- ".join(violations))


@dataclass(frozen=True)
class CoreBuildConfig:
    silver_dir: Path
    exchange_rates_csv: Path
    core_dir: Path
    business_date: date
    business_day_cutoff: time
    fx_max_staleness_days: int
    document_hash_key: str

    @property
    def as_of(self) -> datetime:
        return business_as_of(self.business_date, self.business_day_cutoff)


@dataclass
class CoreBuildReport:
    business_date: str
    as_of: str
    rows: dict[str, int] = field(default_factory=dict)
    transactions: dict[str, Any] = field(default_factory=dict)


def document_hash(document_number: str, key: str) -> str:
    """HMAC-SHA256 of a document number; the number itself is never stored."""
    return hmac.new(key.encode(), document_number.strip().encode(), hashlib.sha256).hexdigest()


def build_core(config: CoreBuildConfig) -> CoreBuildReport:
    if not config.document_hash_key:
        raise ValueError("DOCUMENT_HASH_KEY is required to store customer documents")
    con = duckdb.connect()
    con.execute("PRAGMA disable_progress_bar")
    try:
        _register_functions(con, config)
        _read_inputs(con, config)
        _validate(con, config)
        _build_tables(con, config)
        return _write(con, config)
    finally:
        con.close()


# ---------------------------------------------------------------------------


def _register_functions(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> None:
    key = config.document_hash_key
    con.create_function(
        "document_hmac",
        lambda value: None if value is None else document_hash(value, key),
        ["VARCHAR"],
        "VARCHAR",
        null_handling=FunctionNullHandling.SPECIAL,
    )
    con.create_function("age_band_of", age_band, ["INTEGER"], "VARCHAR")


# Column types inferred from CSV depend on the values present (an all-empty column is VARCHAR),
# so numeric and date columns are cast explicitly. A value that does not parse becomes NULL and
# is caught by the data contract (amounts) or fails loudly (dates, scores).
_SILVER_TYPES = {
    "transactions": """REPLACE (
        TRY_CAST(amount AS DOUBLE) AS amount,
        TRY_CAST(amount_usd AS DOUBLE) AS amount_usd,
        CAST(fraud_score AS DOUBLE) AS fraud_score,
        CAST(transaction_date AS TIMESTAMP) AS transaction_date,
        CAST(process_date AS DATE) AS process_date
    )""",
}


def _literal(path: Path) -> str:
    return "'" + path.as_posix().replace("'", "''") + "'"


def _read_inputs(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> None:
    for table in ("customers", "products", "transactions"):
        path = config.silver_dir / f"{table}.parquet"
        if not path.exists():
            raise FileNotFoundError(f"Missing silver file {path}; run scripts/ingest.py {table}")
        con.execute(
            f"CREATE TABLE silver_{table} AS SELECT * {_SILVER_TYPES.get(table, '')} "
            f"FROM read_parquet({_literal(path)})"
        )
    if not config.exchange_rates_csv.exists():
        raise FileNotFoundError(f"Missing exchange rates file {config.exchange_rates_csv}")
    con.execute(
        f"""
        CREATE TABLE usd_rates AS
        SELECT CAST(date AS DATE) AS rate_date, source_currency AS currency,
               CAST(exchange_rate AS DOUBLE) AS rate
        FROM read_csv({_literal(config.exchange_rates_csv)}, header = true)
        WHERE target_currency = 'USD'
        """
    )


def _not_in(column: str, values: frozenset[str]) -> str:
    listed = ", ".join(f"'{value}'" for value in sorted(values))
    return f"{column} IS NULL OR {column} NOT IN ({listed})"


def _validate(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> None:
    business_date = config.business_date.isoformat()
    checks: list[tuple[str, str, str, str]] = [
        # (description, table, id column, condition that marks a bad row)
        ("customers without customer_id", "silver_customers", "customer_id", "customer_id IS NULL"),
        (
            "customer_status outside the expected set",
            "silver_customers",
            "customer_id",
            _not_in("customer_status", CUSTOMER_STATUSES),
        ),
        (
            "customers without document_number",
            "silver_customers",
            "customer_id",
            "document_number IS NULL",
        ),
        ("products without product_id", "silver_products", "product_id", "product_id IS NULL"),
        (
            "product_type outside the expected set",
            "silver_products",
            "product_id",
            _not_in("product_type", PRODUCT_TYPES),
        ),
        (
            "product_status outside the expected set",
            "silver_products",
            "product_id",
            _not_in("product_status", PRODUCT_STATUSES),
        ),
        (
            "product currency outside the expected set",
            "silver_products",
            "product_id",
            _not_in("currency", CURRENCIES),
        ),
        (
            "product_number without four final digits",
            "silver_products",
            "product_id",
            "NOT regexp_matches(CAST(product_number AS VARCHAR), '[0-9]{4}$')",
        ),
        (
            "products of an unknown customer",
            "silver_products",
            "product_id",
            "customer_id NOT IN (SELECT customer_id FROM silver_customers)",
        ),
        (
            "transactions without transaction_id",
            "silver_transactions",
            "transaction_id",
            "transaction_id IS NULL",
        ),
        (
            "transaction_type outside the expected set",
            "silver_transactions",
            "transaction_id",
            _not_in("transaction_type", TRANSACTION_TYPES),
        ),
        (
            "transaction_status outside the expected set",
            "silver_transactions",
            "transaction_id",
            _not_in("transaction_status", TRANSACTION_STATUSES),
        ),
        (
            "transaction currency outside the expected set",
            "silver_transactions",
            "transaction_id",
            _not_in("currency", CURRENCIES),
        ),
        (
            "transactions without a positive amount",
            "silver_transactions",
            "transaction_id",
            "amount IS NULL OR amount <= 0",
        ),
        (
            "transactions without transaction_date or process_date",
            "silver_transactions",
            "transaction_id",
            "transaction_date IS NULL OR process_date IS NULL",
        ),
        (
            f"process_date after BUSINESS_DATE ({business_date})",
            "silver_transactions",
            "transaction_id",
            f"CAST(process_date AS DATE) > DATE '{business_date}'",
        ),
        (
            "fraud_score outside 0-100",
            "silver_transactions",
            "transaction_id",
            "fraud_score < 0 OR fraud_score > 100",
        ),
        (
            "transactions of an unknown product or of another customer's product",
            "silver_transactions",
            "transaction_id",
            "NOT EXISTS (SELECT 1 FROM silver_products p WHERE p.product_id = "
            "silver_transactions.product_id AND p.customer_id = silver_transactions.customer_id)",
        ),
    ]
    violations: list[str] = []
    for description, table, id_column, condition in checks:
        bad = con.execute(f"SELECT count(*) FROM {table} WHERE {condition}").fetchone()
        count = bad[0] if bad else 0
        if count:
            samples = [
                str(row[0])
                for row in con.execute(
                    f"SELECT {id_column} FROM {table} WHERE {condition} "
                    f"ORDER BY {id_column} NULLS FIRST LIMIT {SAMPLE_SIZE}"
                ).fetchall()
            ]
            violations.append(f"{description}: {count} row(s), e.g. {samples}")
    if violations:
        raise DataContractError(violations)


def _lineage(column: str = "_source_file") -> str:
    # Keep the path from the raw folder on, so lineage does not depend on the machine.
    return (
        f"regexp_replace(replace(CAST({column} AS VARCHAR), '\\', '/'), '^.*?/(raw/)', '\\1') "
        "AS source_file, CAST(_ingested_at AS TIMESTAMP) AS ingested_at"
    )


def _build_tables(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> None:
    bd = config.business_date.isoformat()
    con.execute(
        f"""
        CREATE TABLE core_customers AS
        WITH src AS (
            SELECT *, CAST(date_of_birth AS DATE) AS dob FROM silver_customers
        )
        SELECT
            customer_id,
            CAST(document_type AS VARCHAR) AS document_type,
            document_hmac(CAST(document_number AS VARCHAR)) AS document_hash,
            customer_status,
            country,
            CAST(registration_date AS TIMESTAMP) AS registration_date,
            CAST(last_updated AS TIMESTAMP) AS last_updated,
            CAST(gender AS VARCHAR) AS gender,
            CASE WHEN dob IS NULL THEN NULL ELSE age_band_of(CAST(
                year(DATE '{bd}') - year(dob)
                - CASE WHEN month(DATE '{bd}') * 100 + day(DATE '{bd}')
                            < month(dob) * 100 + day(dob) THEN 1 ELSE 0 END
            AS INTEGER)) END AS age_band,
            detected_accent,
            segment,
            CAST(credit_score AS DECIMAL(6, 1)) AS credit_score,
            CAST(estimated_monthly_income AS DECIMAL(18, 2)) AS estimated_monthly_income,
            occupation,
            marital_status,
            education_level,
            {_lineage()}
        FROM src
        """
    )
    con.execute(
        f"""
        CREATE TABLE core_products AS
        SELECT
            product_id,
            customer_id,
            product_type,
            right(CAST(product_number AS VARCHAR), 4) AS product_number_last4,
            currency,
            product_status,
            CAST(opening_date AS DATE) AS opening_date,
            CAST(expiration_date AS DATE) AS expiration_date,
            CAST(last_updated AS TIMESTAMP) AS last_updated,
            {_lineage()}
        FROM silver_products
        """
    )
    staleness = int(config.fx_max_staleness_days)
    con.execute(
        f"""
        CREATE TABLE core_transactions AS
        WITH tx AS (
            SELECT *, CAST(CAST(transaction_date AS TIMESTAMP) AS DATE) AS tx_day
            FROM silver_transactions
        ),
        priced AS (
            SELECT tx.*, r.rate_date, r.rate
            FROM tx ASOF LEFT JOIN usd_rates r
              ON tx.currency = r.currency AND tx.tx_day >= r.rate_date
        ),
        resolved AS (
            SELECT *,
                CASE
                    WHEN amount_usd IS NOT NULL THEN '{AmountUsdSource.SOURCE}'
                    WHEN currency = 'USD' THEN '{AmountUsdSource.IDENTITY}'
                    WHEN rate_date IS NOT NULL
                         AND date_diff('day', rate_date, tx_day) <= {staleness}
                        THEN '{AmountUsdSource.FX_RATE}'
                    ELSE '{AmountUsdSource.MISSING}'
                END AS usd_source
            FROM priced
        )
        SELECT
            transaction_id,
            customer_id,
            product_id,
            CAST(transaction_date AS TIMESTAMP) AS transaction_date,
            CAST(process_date AS DATE) AS process_date,
            transaction_type,
            CAST(transaction_category AS VARCHAR) AS transaction_category,
            CAST(amount AS DECIMAL(18, 2)) AS amount,
            currency,
            CAST(CASE usd_source
                WHEN '{AmountUsdSource.SOURCE}' THEN amount_usd
                WHEN '{AmountUsdSource.IDENTITY}' THEN amount
                WHEN '{AmountUsdSource.FX_RATE}' THEN amount * rate
            END AS DECIMAL(18, 2)) AS amount_usd,
            usd_source AS amount_usd_source,
            CASE WHEN usd_source = '{AmountUsdSource.FX_RATE}' THEN rate_date END
                AS fx_rate_date,
            CAST(channel AS VARCHAR) AS channel,
            CAST(merchant_name AS VARCHAR) AS merchant_name,
            CAST(merchant_category AS VARCHAR) AS merchant_category,
            transaction_status,
            CAST(response_code AS VARCHAR) AS response_code,
            CAST(fraud_score AS DECIMAL(5, 2)) AS fraud_score,
            CAST(is_fraud AS BOOLEAN) AS is_fraud,
            {_lineage()}
        FROM resolved
        """
    )
    duplicated = con.execute(
        "SELECT count(*) FROM (SELECT document_hash FROM core_customers "
        "WHERE document_hash IS NOT NULL GROUP BY 1 HAVING count(*) > 1)"
    ).fetchone()
    if duplicated and duplicated[0]:
        raise DataContractError([f"document numbers shared by several customers: {duplicated[0]}"])


def _write(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> CoreBuildReport:
    config.core_dir.mkdir(parents=True, exist_ok=True)
    report = CoreBuildReport(
        business_date=config.business_date.isoformat(), as_of=config.as_of.isoformat()
    )
    for table in CORE_BANKING_TABLES:
        columns = [column.name for column in table.columns]
        built = [row[0] for row in con.execute(f"DESCRIBE core_{table.name}").fetchall()]
        if built != columns:
            raise RuntimeError(f"core_{table.name} columns {built} do not match {columns}")
        out = config.core_dir / f"{table.name}.parquet"
        con.execute(
            f"COPY (SELECT * FROM core_{table.name} ORDER BY 1) TO '{out.as_posix()}' "
            "(FORMAT parquet, COMPRESSION zstd)"
        )
        count = con.execute(f"SELECT count(*) FROM core_{table.name}").fetchone()
        report.rows[table.name] = count[0] if count else 0
    report.transactions = _transaction_figures(con, config)
    return report


def _transaction_figures(con: duckdb.DuckDBPyConnection, config: CoreBuildConfig) -> dict[str, Any]:
    def scalar(sql: str) -> Any:
        row = con.execute(sql).fetchone()
        return row[0] if row else None

    total = scalar("SELECT count(*) FROM core_transactions") or 0
    sources = dict(
        con.execute(
            "SELECT amount_usd_source, count(*) FROM core_transactions GROUP BY 1 ORDER BY 1"
        ).fetchall()
    )
    stale = {
        str(days): count
        for days, count in con.execute(
            "SELECT date_diff('day', fx_rate_date, CAST(transaction_date AS DATE)) AS d, count(*) "
            "FROM core_transactions WHERE amount_usd_source = 'fx_rate' GROUP BY 1 ORDER BY 1"
        ).fetchall()
    }
    as_of = config.as_of.isoformat(sep=" ")
    return {
        "total": total,
        "process_date_range": [
            str(scalar("SELECT min(process_date) FROM core_transactions")),
            str(scalar("SELECT max(process_date) FROM core_transactions")),
        ],
        "partitions": scalar("SELECT count(DISTINCT process_date) FROM core_transactions"),
        "amount_usd_source": sources,
        # Days between the rate used and the transaction date; "0" means same-day rate.
        "fx_rate_age_days": stale,
        "fx_rate_from_earlier_day": sum(count for days, count in stale.items() if int(days) > 0),
        "late_arrivals": scalar(
            "SELECT count(*) FROM core_transactions "
            "WHERE CAST(transaction_date AS DATE) < process_date"
        ),
        "after_midnight_of_process_date": scalar(
            "SELECT count(*) FROM core_transactions "
            "WHERE CAST(transaction_date AS DATE) > process_date"
        ),
        "after_as_of": scalar(
            f"SELECT count(*) FROM core_transactions WHERE transaction_date >= TIMESTAMP '{as_of}'"
        ),
        "fraud_score_null": scalar(
            "SELECT count(*) FROM core_transactions WHERE fraud_score IS NULL"
        ),
    }
