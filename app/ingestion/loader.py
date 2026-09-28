"""Core Parquet -> PostgreSQL (Core Banking tables).

Idempotent: rows are copied into a temporary staging table and upserted by primary key, so
loading the same files twice leaves the same rows. The loader never commits; the caller owns
the transaction (the CLI commits, tests roll back).
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import duckdb
import psycopg
from psycopg import sql
from sqlalchemy import Table

from app.storage.models import CORE_BANKING_TABLES

COPY_CHUNK_BYTES = 1 << 20
NULL_MARKER = r"\N"


@dataclass(frozen=True)
class TableLoad:
    table: str
    rows_in_file: int
    rows_before: int
    rows_after: int


def load_core_banking(connection: psycopg.Connection, core_dir: Path) -> list[TableLoad]:
    """Upsert customers, products and transactions, in foreign-key order."""
    return [_load_table(connection, table, core_dir) for table in CORE_BANKING_TABLES]


def _count(connection: psycopg.Connection, table: str) -> int:
    row = connection.execute(
        sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
    ).fetchone()
    return int(row[0]) if row else 0


def _load_table(connection: psycopg.Connection, table: Table, core_dir: Path) -> TableLoad:
    parquet = core_dir / f"{table.name}.parquet"
    if not parquet.exists():
        raise FileNotFoundError(f"Missing core file {parquet}; build it first")
    columns = [column.name for column in table.columns]
    keys = [column.name for column in table.primary_key.columns]

    with tempfile.TemporaryDirectory() as tmp:
        csv_path = Path(tmp) / f"{table.name}.csv"
        con = duckdb.connect()
        try:
            file_columns = [
                row[0]
                for row in con.execute(
                    "DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)]
                ).fetchall()
            ]
            if file_columns != columns:
                raise ValueError(f"{parquet} columns {file_columns} do not match {columns}")
            con.execute(
                f"COPY (SELECT * FROM read_parquet('{parquet.as_posix()}')) "
                f"TO '{csv_path.as_posix()}' (HEADER, DELIMITER ',', NULLSTR '{NULL_MARKER}')"
            )
            counted = con.execute("SELECT count(*) FROM read_parquet(?)", [str(parquet)]).fetchone()
            rows_in_file = int(counted[0]) if counted else 0
        finally:
            con.close()

        staging = sql.Identifier(f"staging_{table.name}")
        target = sql.Identifier(table.name)
        column_list = sql.SQL(", ").join(sql.Identifier(name) for name in columns)
        rows_before = _count(connection, table.name)

        connection.execute(sql.SQL("DROP TABLE IF EXISTS {}").format(staging))
        connection.execute(
            sql.SQL("CREATE TEMP TABLE {} (LIKE {} INCLUDING DEFAULTS)").format(staging, target)
        )
        copy_statement = sql.SQL(
            "COPY {} ({}) FROM STDIN WITH (FORMAT csv, HEADER true, NULL {})"
        ).format(staging, column_list, sql.Literal(NULL_MARKER))
        with (
            connection.cursor() as cursor,
            cursor.copy(copy_statement) as copy,
            csv_path.open("rb") as handle,
        ):
            while chunk := handle.read(COPY_CHUNK_BYTES):
                copy.write(chunk)

    updates = sql.SQL(", ").join(
        sql.SQL("{} = EXCLUDED.{}").format(sql.Identifier(name), sql.Identifier(name))
        for name in columns
        if name not in keys
    )
    connection.execute(
        sql.SQL(
            "INSERT INTO {target} ({columns}) SELECT {columns} FROM {staging} "
            "ON CONFLICT ({keys}) DO UPDATE SET {updates}"
        ).format(
            target=target,
            columns=column_list,
            staging=staging,
            keys=sql.SQL(", ").join(sql.Identifier(name) for name in keys),
            updates=updates,
        )
    )
    connection.execute(sql.SQL("DROP TABLE {}").format(staging))
    return TableLoad(
        table=table.name,
        rows_in_file=rows_in_file,
        rows_before=rows_before,
        rows_after=_count(connection, table.name),
    )
