"""Silver -> core Parquet: USD conversion, minimization, data contract and report."""

from __future__ import annotations

import shutil
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import pytest

from app.ingestion.core import DataContractError, build_core
from app.storage.data_contract import document_hash
from app.storage.models import CORE_BANKING_TABLES
from tests.conftest import TEST_HASH_KEY, Pipeline, core_config, run_ingest
from tests.fixtures.core_banking import (
    CUSTOMER,
    CUSTOMERS,
    PRODUCTS,
    TRANSACTIONS,
    Tx,
    write_raw_dataset,
)


def rows(pipeline: Pipeline, table: str, where: str = "TRUE") -> list[dict[str, Any]]:
    con = duckdb.connect()
    try:
        path = (pipeline.core_dir / f"{table}.parquet").as_posix()
        result = con.execute(f"SELECT * FROM '{path}' WHERE {where}")
        names = [d[0] for d in result.description]
        return [dict(zip(names, row, strict=True)) for row in result.fetchall()]
    finally:
        con.close()


def tx(pipeline: Pipeline, transaction_id: str) -> dict[str, Any]:
    (row,) = rows(pipeline, "transactions", f"transaction_id = '{transaction_id}'")
    return row


# --- USD equivalent ---------------------------------------------------------


def test_supplied_amount_usd_is_kept(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-COP-SOURCE")
    assert row["amount_usd_source"] == "source"
    assert row["amount_usd"] == Decimal("100.00")
    assert row["fx_rate_date"] is None


def test_usd_transactions_use_identity(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-T1-PURCHASE")
    assert row["amount_usd_source"] == "identity"
    assert row["amount_usd"] == row["amount"] == Decimal("50.00")


def test_same_day_rate(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-COP-FX")
    assert row["amount_usd_source"] == "fx_rate"
    assert row["amount_usd"] == Decimal("50.00")  # 200,000 COP x 0.00025
    assert row["fx_rate_date"] == date(2026, 6, 17)


def test_latest_rate_within_staleness_margin(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-ARS-STALE")  # 2026-06-14, last ARS rate 2026-06-12
    assert row["amount_usd_source"] == "fx_rate"
    assert row["fx_rate_date"] == date(2026, 6, 12)
    assert row["amount_usd"] == Decimal("30.00")


def test_rate_older_than_margin_is_missing(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-ARS-MISSING")  # 5 days after the last rate
    assert row["amount_usd_source"] == "missing"
    assert row["amount_usd"] is None
    assert row["fx_rate_date"] is None


def test_staleness_margin_is_inclusive(tmp_path: Path) -> None:
    # 2026-06-15 is exactly 3 days after the last ARS rate (2026-06-12).
    boundary = Tx(
        "TRX-ARS-3DAYS",
        "2026-06-15 10:00:00",
        "2026-06-15",
        "PRD-ARSALPHA0001",
        CUSTOMER,
        "Withdrawal",
        "1000.00",
        "ARS",
    )
    four = Tx(
        "TRX-ARS-4DAYS",
        "2026-06-16 10:00:00",
        "2026-06-16",
        "PRD-ARSALPHA0001",
        CUSTOMER,
        "Withdrawal",
        "1000.00",
        "ARS",
    )
    pipe = _build(tmp_path, transactions=[boundary, four])
    assert tx(pipe, "TRX-ARS-3DAYS")["amount_usd_source"] == "fx_rate"
    assert tx(pipe, "TRX-ARS-4DAYS")["amount_usd_source"] == "missing"


def test_zero_staleness_requires_same_day_rate(tmp_path: Path) -> None:
    pipe = _build(tmp_path, fx_max_staleness_days=0)
    assert tx(pipe, "TRX-ARS-STALE")["amount_usd_source"] == "missing"
    assert tx(pipe, "TRX-COP-FX")["amount_usd_source"] == "fx_rate"


# --- Timestamps and lineage -------------------------------------------------


def test_timestamps_are_naive_and_unchanged(pipeline: Pipeline) -> None:
    row = tx(pipeline, "TRX-NEXT-DAY")
    assert row["transaction_date"] == datetime(2026, 6, 18, 3, 0)
    assert row["transaction_date"].tzinfo is None
    assert row["process_date"] == date(2026, 6, 17)


def test_lineage_is_relative_to_the_raw_folder(pipeline: Pipeline) -> None:
    assert tx(pipeline, "TRX-T1-PURCHASE")["source_file"] == (
        "raw/transactions/year=2026/month=06/day=17/transactions_20260617.csv"
    )
    (customer,) = rows(pipeline, "customers", f"customer_id = '{CUSTOMER}'")
    assert customer["source_file"] == "raw/customers.csv"
    assert customer["ingested_at"] is not None


# --- Minimization -----------------------------------------------------------


PERSONAL = {
    "document_number",
    "first_name",
    "last_name",
    "date_of_birth",
    "email",
    "mobile_phone",
    "landline_phone",
    "address",
    "city",
    "state",
    "postal_code",
    "product_number",
    "current_balance",
    "latitude",
    "longitude",
}


@pytest.mark.parametrize("table", [t.name for t in CORE_BANKING_TABLES])
def test_core_columns_match_the_database_and_exclude_personal_data(
    pipeline: Pipeline, table: str
) -> None:
    (model,) = [t for t in CORE_BANKING_TABLES if t.name == table]
    columns = list(rows(pipeline, table)[0])
    assert columns == [c.name for c in model.columns]
    assert not PERSONAL & set(columns)


def test_personal_values_do_not_appear_anywhere(pipeline: Pipeline) -> None:
    dumped = " ".join(
        str(value)
        for table in ("customers", "products")
        for row in rows(pipeline, table)
        for value in row.values()
    )
    for secret in (
        "X1234567",
        "Ana",
        "ana@example.test",
        "Calle Falsa",
        "4111111111114821",
        "1990-06-18",
    ):
        assert secret not in dumped


def test_document_is_stored_as_keyed_hash(pipeline: Pipeline) -> None:
    (customer,) = rows(pipeline, "customers", f"customer_id = '{CUSTOMER}'")
    assert customer["document_hash"] == document_hash("X1234567", TEST_HASH_KEY)
    assert customer["document_hash"] != document_hash("X1234567", "another-key")
    assert len(customer["document_hash"]) == 64


def test_age_band_is_computed_at_the_business_date(pipeline: Pipeline) -> None:
    bands = {row["customer_id"]: row["age_band"] for row in rows(pipeline, "customers")}
    # Born 1990-06-18: still 35 on 2026-06-17.
    assert bands[CUSTOMER] == "35-44"
    assert bands["CLI-BETA00000002"] == "65+"
    assert bands["CLI-GAMMA0000003"] == "18-24"


def test_product_keeps_only_last_four_digits(pipeline: Pipeline) -> None:
    last4 = {row["product_id"]: row["product_number_last4"] for row in rows(pipeline, "products")}
    assert last4["PRD-CARDALPHA001"] == "4821"
    assert last4["PRD-ACCTALPHA001"] == "1960"


# --- Report -----------------------------------------------------------------


def test_report_figures(pipeline: Pipeline) -> None:
    report = pipeline.report
    assert report.business_date == "2026-06-17"
    assert report.as_of == "2026-06-18T06:00:00"
    assert report.rows == {"customers": 3, "products": len(PRODUCTS), "transactions": 17}
    figures = report.transactions
    assert figures["amount_usd_source"] == {
        "fx_rate": 2,
        "identity": 12,
        "missing": 1,
        "source": 2,
    }
    assert figures["fx_rate_age_days"] == {"0": 1, "2": 1}
    assert figures["fx_rate_from_earlier_day"] == 1
    assert figures["late_arrivals"] == 1  # TRX-T1-PURCHASE, reposted in the next partition
    assert figures["after_midnight_of_process_date"] == 2  # TRX-NEXT-DAY, TRX-DUP-SECOND
    assert figures["after_as_of"] == 0
    assert figures["fraud_score_null"] == 1
    assert figures["partitions"] == 4
    assert figures["process_date_range"] == ["2026-06-14", "2026-06-17"]


# --- Data contract failures -------------------------------------------------


def _build(tmp_path: Path, **kwargs: Any) -> Pipeline:
    config_keys = {"fx_max_staleness_days", "business_date", "document_hash_key"}
    config_overrides = {k: v for k, v in kwargs.items() if k in config_keys}
    raw_overrides = {k: v for k, v in kwargs.items() if k not in config_keys}
    raw = write_raw_dataset(tmp_path, **raw_overrides)
    result = run_ingest(tmp_path, "customers", "products", "transactions")
    assert result.returncode == 0, result.stderr
    report = build_core(core_config(tmp_path, **config_overrides))
    return Pipeline(tmp_path, raw, tmp_path / "data" / "silver", tmp_path / "data" / "core", report)


def _violation(tmp_path: Path, **kwargs: Any) -> DataContractError:
    with pytest.raises(DataContractError) as info:
        _build(tmp_path, **kwargs)
    return info.value


def _replace(tx_: Tx, **changes: str) -> Tx:
    return Tx(**{**tx_.__dict__, **changes})


BASE = TRANSACTIONS[1]  # TRX-T3-PURCHASE


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"transaction_type": "Compra"}, "transaction_type outside the expected set"),
        ({"status": "Aprobada"}, "transaction_status outside the expected set"),
        ({"currency": "EUR"}, "transaction currency outside the expected set"),
        ({"amount": "0"}, "without a positive amount"),
        ({"fraud_score": "120"}, "fraud_score outside 0-100"),
        ({"product_id": "PRD-UNKNOWN00001"}, "unknown product"),
        ({"product_id": "PRD-CARDBETA0001"}, "another customer's product"),
    ],
)
def test_transaction_contract_violations(
    tmp_path: Path, changes: dict[str, str], message: str
) -> None:
    error = _violation(tmp_path, transactions=[_replace(BASE, **changes)])
    assert any(message in v for v in error.violations), error.violations
    assert "TRX-T3-PURCHASE" in str(error)


def test_process_date_after_business_date_fails(tmp_path: Path) -> None:
    late = _replace(BASE, process_date="2026-06-18", at="2026-06-18 10:00:00")
    error = _violation(tmp_path, transactions=[late])
    assert any("process_date after BUSINESS_DATE (2026-06-17)" in v for v in error.violations)


def test_transaction_date_after_business_date_is_accepted(tmp_path: Path) -> None:
    # Policy: only process_date is checked; 03:00 next day belongs to the business day.
    pipe = _build(tmp_path, transactions=[TRANSACTIONS[12]])
    assert pipe.report.rows["transactions"] == 1


def test_every_violation_is_reported_at_once(tmp_path: Path) -> None:
    bad = [
        _replace(BASE, transaction_type="Compra"),
        _replace(BASE, transaction_id="TRX-OTHER", status="Aprobada"),
    ]
    error = _violation(tmp_path, transactions=bad)
    assert len(error.violations) == 2


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("product_type", "Tarjeta Prepago", "product_type outside the expected set"),
        ("product_status", "Frozen", "product_status outside the expected set"),
        ("currency", "MXN", "product currency outside the expected set"),
        ("product_number", "12ab", "four final digits"),
        ("customer_id", "CLI-NOBODY000000", "unknown customer"),
    ],
)
def test_product_contract_violations(tmp_path: Path, field: str, value: str, message: str) -> None:
    products = [dict(PRODUCTS[0], **{field: value}), *PRODUCTS[1:]]
    error = _violation(tmp_path, products=products)
    assert any(message in v for v in error.violations), error.violations


def test_customer_status_outside_the_set_fails(tmp_path: Path) -> None:
    customers = [dict(CUSTOMERS[0], customer_status="Activo"), *CUSTOMERS[1:]]
    error = _violation(tmp_path, customers=customers)
    assert any("customer_status outside the expected set" in v for v in error.violations)


def test_customer_without_document_fails(tmp_path: Path) -> None:
    customers = [dict(CUSTOMERS[0], document_number=""), *CUSTOMERS[1:]]
    error = _violation(tmp_path, customers=customers)
    assert any("without document_number" in v for v in error.violations)


def test_shared_document_number_fails(tmp_path: Path) -> None:
    customers = [CUSTOMERS[0], dict(CUSTOMERS[1], document_number="X1234567"), CUSTOMERS[2]]
    error = _violation(tmp_path, customers=customers)
    assert any("shared by several customers" in v for v in error.violations)


# --- Configuration and inputs -----------------------------------------------


def test_hash_key_is_required(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DOCUMENT_HASH_KEY"):
        build_core(core_config(tmp_path, document_hash_key=""))


def test_missing_silver_file_fails(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"scripts/ingest.py customers"):
        build_core(core_config(tmp_path))


def test_missing_rates_file_fails(pipeline: Pipeline, tmp_path: Path) -> None:
    silver = tmp_path / "data" / "silver"
    shutil.copytree(pipeline.silver_dir, silver)
    with pytest.raises(FileNotFoundError, match="exchange rates"):
        build_core(core_config(tmp_path))


def test_rebuild_is_deterministic(pipeline: Pipeline, tmp_path: Path) -> None:
    config = core_config(pipeline.root, core_dir=tmp_path / "core")
    again = build_core(config)
    assert again.rows == pipeline.report.rows
    assert again.transactions == pipeline.report.transactions
