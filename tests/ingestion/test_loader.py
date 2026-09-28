"""Core Parquet -> PostgreSQL loader."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path

import duckdb
import psycopg
import pytest
from sqlalchemy.orm import Session

from app.ingestion.loader import load_core_banking
from app.storage.models import Customer, Transaction
from tests.conftest import Pipeline
from tests.fixtures.core_banking import CUSTOMER, PRODUCTS


def test_first_load_inserts_every_row(pipeline: Pipeline, raw_psycopg: psycopg.Connection) -> None:
    loads = {load.table: load for load in load_core_banking(raw_psycopg, pipeline.core_dir)}
    assert list(loads) == ["customers", "products", "transactions"]
    for load in loads.values():
        assert load.rows_before == 0
        assert load.rows_after == load.rows_in_file == pipeline.report.rows[load.table]
    assert loads["products"].rows_after == len(PRODUCTS)


def test_loading_twice_does_not_duplicate(
    pipeline: Pipeline, raw_psycopg: psycopg.Connection
) -> None:
    load_core_banking(raw_psycopg, pipeline.core_dir)
    again = load_core_banking(raw_psycopg, pipeline.core_dir)
    for load in again:
        assert load.rows_before == load.rows_after == load.rows_in_file


def test_reload_updates_changed_rows(
    pipeline: Pipeline, raw_psycopg: psycopg.Connection, db_session: Session, tmp_path: Path
) -> None:
    load_core_banking(raw_psycopg, pipeline.core_dir)
    changed = tmp_path / "core"
    changed.mkdir()
    con = duckdb.connect()
    for name in ("customers", "products", "transactions"):
        source = (pipeline.core_dir / f"{name}.parquet").as_posix()
        target = (changed / f"{name}.parquet").as_posix()
        update = (
            "REPLACE (CASE WHEN customer_id = 'CLI-ALPHA0000001' THEN 'Suspended' "
            "ELSE customer_status END AS customer_status)"
            if name == "customers"
            else ""
        )
        con.execute(f"COPY (SELECT * {update} FROM '{source}') TO '{target}' (FORMAT parquet)")
    con.close()
    loads = load_core_banking(raw_psycopg, changed)
    assert loads[0].rows_before == loads[0].rows_after
    db_session.expire_all()
    stored = db_session.get(Customer, CUSTOMER)
    assert stored is not None
    assert stored.customer_status == "Suspended"


def test_loaded_values_keep_types_nulls_and_lineage(loaded: Pipeline, db_session: Session) -> None:
    stored = db_session.get(Transaction, "TRX-ARS-MISSING")
    assert stored is not None
    assert stored.amount_usd is None
    assert stored.amount_usd_source == "missing"
    assert stored.amount == Decimal("40000.00")
    fx = db_session.get(Transaction, "TRX-ARS-STALE")
    assert fx is not None
    assert str(fx.fx_rate_date) == "2026-06-12"
    next_day = db_session.get(Transaction, "TRX-NEXT-DAY")
    assert next_day is not None
    assert next_day.transaction_date == datetime(2026, 6, 18, 3, 0)
    assert next_day.merchant_name == "Farmacia Noche"
    assert next_day.source_file.startswith("raw/transactions/")
    no_score = db_session.get(Transaction, "TRX-NO-SCORE")
    assert no_score is not None
    assert no_score.fraud_score is None
    assert no_score.is_fraud is False


def test_missing_core_file_fails(raw_psycopg: psycopg.Connection, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="build it first"):
        load_core_banking(raw_psycopg, tmp_path)


def test_unexpected_columns_fail(
    pipeline: Pipeline, raw_psycopg: psycopg.Connection, tmp_path: Path
) -> None:
    source = (pipeline.core_dir / "customers.parquet").as_posix()
    target = (tmp_path / "customers.parquet").as_posix()
    con = duckdb.connect()
    con.execute(
        f"COPY (SELECT *, 'Ana' AS first_name FROM '{source}') TO '{target}' (FORMAT parquet)"
    )
    con.close()
    with pytest.raises(ValueError, match="do not match"):
        load_core_banking(raw_psycopg, tmp_path)
