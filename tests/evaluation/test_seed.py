"""M17 seeding on the test database: idempotent, a clean --remove, the owner role only, the
Core Banking loader never overwrites SEED- rows, and a seeded customer logs in for real."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb
import psycopg
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import load_policy_config
from app.evaluation.scenarios import Scenario, load_scenarios
from app.evaluation.seed import (
    SeedRemovalError,
    SeedRoleError,
    SeedRows,
    build_rows,
    count_seeded,
    lineage,
    remove,
    require_owner,
    require_owner_url,
    seed,
)
from app.identity.service import IdentityService
from app.ingestion.loader import load_core_banking
from app.main import create_app
from app.storage.models import AuditLog, Case, Customer, Product, SessionRow, Transaction
from tests.conftest import TEST_HASH_KEY, Pipeline
from tests.identity.conftest import OTP, FakeClock, generous_limits, identity_config

ROOT = Path(__file__).resolve().parent.parent.parent
BUSINESS_DATE = date(2026, 6, 17)
LOADED_AT = datetime(2026, 10, 2, 9, 0)


@pytest.fixture
def session_factory(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture(scope="module")
def scenarios() -> list[Scenario]:
    return load_scenarios()


@pytest.fixture(scope="module")
def rows(scenarios: list[Scenario]) -> SeedRows:
    return build_rows(scenarios, BUSINESS_DATE, TEST_HASH_KEY, "test", ingested_at=LOADED_AT)


def real_counts(db: Session) -> dict[str, int]:
    return {
        "customers": db.scalar(select(func.count()).where(~Customer.customer_id.like("SEED-%")))
        or 0,
        "products": db.scalar(select(func.count()).where(~Product.product_id.like("SEED-%"))) or 0,
        "transactions": db.scalar(
            select(func.count()).where(~Transaction.transaction_id.like("SEED-%"))
        )
        or 0,
    }


def test_seeding_writes_every_case(
    loaded: Pipeline, db_session: Session, rows: SeedRows, scenarios: list[Scenario]
) -> None:
    seed(db_session, rows)
    counts = count_seeded(db_session)
    foreign = sum(1 for s in scenarios if any(t.foreign for t in s.transactions))
    assert counts["customers"] == len(scenarios) + foreign
    assert counts["transactions"] == sum(len(s.transactions) for s in scenarios)
    assert counts["cases"] == sum(len(s.prior_cases) for s in scenarios)
    first = db_session.get(Customer, scenarios[0].customer_id)
    assert first is not None and first.source_file == lineage("test")
    assert first.document_hash and len(first.document_hash) == 64  # the HMAC, never the number
    assert {
        c.country
        for c in db_session.scalars(select(Customer).where(Customer.customer_id.like("SEED-%")))
    } == {"Colombia", "México", "Argentina"}


def test_seeding_is_idempotent(loaded: Pipeline, db_session: Session, rows: SeedRows) -> None:
    seed(db_session, rows)
    first = count_seeded(db_session)
    seed(db_session, rows)
    assert count_seeded(db_session) == first


def _audit(db: Session) -> list[tuple[Any, ...]]:
    rows = db.execute(
        select(
            AuditLog.id,
            AuditLog.event_type,
            AuditLog.trace_id,
            AuditLog.conversation_id,
            AuditLog.session_id,
            AuditLog.payload,
            AuditLog.created_at,
        ).order_by(AuditLog.id)
    )
    return [tuple(row) for row in rows]


def _evaluation_run(db: Session, s: Scenario) -> None:
    """What an M18 run leaves on a seeded customer: a session, a case and a trace."""
    now = datetime.now(UTC)
    db.add(
        SessionRow(
            session_id="SES-RUN",
            customer_id=s.customer_id,
            auth_method="test_otp",
            created_at=now,
            last_activity_at=now,
        )
    )
    db.flush()
    db.add(
        Case(
            case_id="DSP-20261002-000001",
            customer_id=s.customer_id,
            transaction_id=s.transaction_id(s.transactions[0].key),
            reason_code="RC_UNRECOGNIZED",
            status="Open",
            tier="T1",
            amount=Decimal("10"),
            currency="USD",
            amount_usd=Decimal("10"),
            idempotency_key="run:1",
            business_created_at=datetime(2026, 6, 18, 6),
        )
    )
    db.add(
        AuditLog(
            event_type="turn_trace",
            trace_id="TRC-RUN",
            conversation_id="CONV-RUN",
            session_id="SES-RUN",
            payload={"x": 1},
        )
    )
    db.flush()


def test_reseeding_resets_a_run_without_touching_the_audit_trail(
    loaded: Pipeline, db_session: Session, rows: SeedRows, scenarios: list[Scenario]
) -> None:
    seed(db_session, rows)
    _evaluation_run(db_session, scenarios[0])
    before = _audit(db_session)
    report = seed(db_session, rows)
    db_session.expire_all()
    assert _audit(db_session) == before  # not a single audit row changed
    assert report.removed["cases"] >= 1 and report.removed["sessions_revoked"] == 1
    assert db_session.get(Case, "DSP-20261002-000001") is None
    old = db_session.get(SessionRow, "SES-RUN")
    assert old is not None and old.revoked_at is not None  # revoked, not deleted
    assert count_seeded(db_session)["cases"] == len(rows.cases)  # only the specified ones


def test_remove_refuses_while_traces_point_to_seeded_sessions(
    loaded: Pipeline, db_session: Session, rows: SeedRows, scenarios: list[Scenario]
) -> None:
    seed(db_session, rows)
    _evaluation_run(db_session, scenarios[0])
    before = _audit(db_session)
    with pytest.raises(SeedRemovalError, match="append-only") as refused:
        remove(db_session)
    assert "nothing was removed" in str(refused.value) and "new database" in str(refused.value)
    assert _audit(db_session) == before


def test_remove_deletes_only_seeded_rows(
    loaded: Pipeline, db_session: Session, rows: SeedRows
) -> None:
    before = real_counts(db_session)
    seed(db_session, rows)
    removed = remove(db_session)
    assert removed["customers"] == len(rows.customers)
    assert count_seeded(db_session) == {
        "customers": 0,
        "products": 0,
        "transactions": 0,
        "cases": 0,
    }
    assert real_counts(db_session) == before


def test_only_seeded_rows_are_accepted(
    loaded: Pipeline, db_session: Session, rows: SeedRows
) -> None:
    bad = SeedRows(customers=[{**rows.customers[0], "customer_id": "CLI-ALPHA0000001"}])
    with pytest.raises(ValueError, match="not a seeded row"):
        seed(db_session, bad)


# --- The owner role only -----------------------------------------------------------------------


def test_the_owner_url_is_required() -> None:
    app = "postgresql+psycopg://disputes_app:secret@localhost:5432/disputes"
    owner = "postgresql+psycopg://disputes_owner:secret@localhost:5432/disputes"
    with pytest.raises(SeedRoleError, match="not set"):
        require_owner_url(None, app)
    with pytest.raises(SeedRoleError, match="application's role"):
        require_owner_url(app.replace("secret", "other"), app)
    assert require_owner_url(owner, app) == owner


def test_the_connection_must_own_the_tables(loaded: Pipeline, db_session: Session) -> None:
    require_owner(db_session)  # the test database's user owns its tables

    class NotOwner:
        def execute(self, *_: Any) -> Any:
            class Row:
                def one(self) -> tuple[str, str]:
                    return ("disputes_owner", "disputes_app")

            return Row()

    with pytest.raises(SeedRoleError, match="does not own"):
        require_owner(NotOwner())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "env",
    [
        {"MIGRATION_DATABASE_URL": ""},
        {
            "DATABASE_URL": "postgresql+psycopg://app:a@localhost:1/db",
            "MIGRATION_DATABASE_URL": "postgresql+psycopg://app:b@localhost:1/db",
        },
    ],
)
def test_the_script_refuses_without_the_owner_role(env: dict[str, str]) -> None:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "seed_scenarios.py")],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONIOENCODING": "utf-8", **env},
        check=False,
    )
    assert result.returncode != 0
    assert "owner role" in result.stderr or "application's role" in result.stderr


# --- The Core Banking loader -------------------------------------------------------------------


def test_the_loader_never_overwrites_seeded_rows(
    loaded: Pipeline,
    db_session: Session,
    raw_psycopg: psycopg.Connection,
    rows: SeedRows,
    scenarios: list[Scenario],
    tmp_path: Path,
) -> None:
    seed(db_session, rows)
    db_session.flush()
    target = scenarios[0].customer_id
    original = db_session.get(Customer, target)
    assert original is not None
    country = original.country
    core = tmp_path / "core"
    shutil.copytree(loaded.core_dir, core)
    customers = core / "customers.parquet"
    tampered = tmp_path / "customers.parquet"
    con = duckdb.connect()
    source = f"read_parquet('{customers.as_posix()}')"
    replaced = f"'{target}' AS customer_id, NULL AS document_hash, 'Zimbabwe' AS country"
    con.execute(
        f"COPY (SELECT * FROM {source} UNION ALL (SELECT * REPLACE ({replaced}) FROM {source} "
        f"LIMIT 1)) TO '{tampered.as_posix()}' (FORMAT parquet)"
    )
    rows_in_file = con.execute(
        f"SELECT count(*) FROM read_parquet('{tampered.as_posix()}') WHERE customer_id = ?",
        [target],
    ).fetchone()
    con.close()
    assert rows_in_file == (1,)  # the file really carries a row with the seeded ID
    shutil.move(tampered, customers)
    load_core_banking(raw_psycopg, core)
    db_session.expire_all()
    kept = db_session.get(Customer, target)
    assert kept is not None and kept.country == country and kept.source_file == lineage("test")


# --- A seeded customer logs in through the real Identity Service --------------------------------


def test_a_seeded_customer_logs_in_with_its_document(
    loaded: Pipeline,
    db_session: Session,
    rows: SeedRows,
    scenarios: list[Scenario],
    session_factory: sessionmaker[Session],
) -> None:
    seed(db_session, rows)
    db_session.flush()
    identity = IdentityService(identity_config(), session_factory, FakeClock())
    app = create_app(load_policy_config(), identity=identity, rate_limits=generous_limits())
    case = scenarios[0]
    with TestClient(app) as client:
        assert (
            client.post("/auth/login", json={"document_number": case.document_number}).status_code
            == 202
        )
        verified = client.post(
            "/auth/verify", json={"document_number": case.document_number, "otp": OTP}
        )
        assert verified.status_code == 200
        session = identity.validate_session(verified.json()["access_token"])
    assert session is not None and session.customer_id == case.customer_id


def test_another_customers_records_get_only_their_product(scenarios: list[Scenario]) -> None:
    foreign = next(s for s in scenarios if any(t.foreign for t in s.transactions))
    account = foreign.products[0].model_copy(
        update={"key": "acct", "type": "Cuenta Ahorro", "last4": "1111"}
    )
    extra = foreign.model_copy(update={"products": [*foreign.products, account]})
    built = build_rows([extra], BUSINESS_DATE, TEST_HASH_KEY, "test", ingested_at=LOADED_AT)
    theirs = [p for p in built.products if p["customer_id"] == extra.foreign_customer_id]
    assert [p["product_id"] for p in theirs] == [extra.product_id("card") + "X"]
