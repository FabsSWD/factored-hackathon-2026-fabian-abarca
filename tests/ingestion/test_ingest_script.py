"""scripts/ingest.py (raw CSV -> silver Parquet) on the synthetic dataset."""

from __future__ import annotations

import json

import duckdb

from tests.conftest import Pipeline, run_ingest
from tests.fixtures.core_banking import TRANSACTIONS


def _query(sql: str) -> list[tuple[object, ...]]:
    con = duckdb.connect()
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_reads_every_partition_recursively(pipeline: Pipeline) -> None:
    report = json.loads((pipeline.root / "reports" / "quality_transactions.json").read_text())
    partitions = {tx.process_date for tx in TRANSACTIONS}
    assert report["source_files"] == len(partitions)


def test_deduplicates_by_transaction_id_across_partitions(pipeline: Pipeline) -> None:
    silver = (pipeline.silver_dir / "transactions.parquet").as_posix()
    rows = _query(
        f"SELECT amount, process_date FROM '{silver}' WHERE transaction_id = 'TRX-T1-PURCHASE'"
    )
    # The copy in the later partition wins over the stale one.
    assert len(rows) == 1
    assert float(rows[0][0]) == 50.0  # type: ignore[arg-type]
    assert str(rows[0][1]) == "2026-06-17"
    total = _query(f"SELECT count(*), count(DISTINCT transaction_id) FROM '{silver}'")[0]
    assert total[0] == total[1] == len(TRANSACTIONS)


def test_keeps_the_source_file_of_every_row(pipeline: Pipeline) -> None:
    silver = (pipeline.silver_dir / "transactions.parquet").as_posix()
    rows = _query(f"SELECT count(*) FILTER (WHERE _source_file IS NULL) FROM '{silver}'")
    assert rows[0][0] == 0
    sample = _query(
        f"SELECT _source_file FROM '{silver}' WHERE transaction_id = 'TRX-T1-PURCHASE'"
    )[0][0]
    assert (
        str(sample)
        .replace("\\", "/")
        .endswith("transactions/year=2026/month=06/day=17/transactions_20260617.csv")
    )


def test_empty_table_folder_does_not_hide_the_single_file(pipeline: Pipeline) -> None:
    # data/raw/customers/ exists and is empty, as in the supplied data.
    assert (pipeline.raw_dir / "customers").is_dir()
    silver = (pipeline.silver_dir / "customers.parquet").as_posix()
    assert _query(f"SELECT count(*) FROM '{silver}'")[0][0] == 3


def test_unknown_table_is_rejected(pipeline: Pipeline) -> None:
    result = run_ingest(pipeline.root, "nonexistent")
    assert result.returncode != 0
    assert "Unknown tables" in result.stderr
