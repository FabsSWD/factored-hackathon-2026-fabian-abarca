"""M17 cases on real records: reading the records the Tool Layer reads, the engine label on
them, the description that identifies one record, the script filled at run time and the
document read from the raw file. Run on the synthetic test database, never on real data."""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import yaml
from pydantic import ValidationError
from sqlalchemy import Connection
from sqlalchemy.orm import Session

from app.evaluation.real import (
    CATEGORY,
    RealScenario,
    describe_record,
    description_identifies,
    document_for,
    label_as_of,
    label_real,
    load_lock,
    load_real_labels,
    load_real_scenarios,
    read_records,
    render_script,
    selection_digest,
    spoken_amount,
    without_records,
)
from app.storage.models import Transaction
from tests.conftest import Pipeline
from tests.fixtures.core_banking import CARD, CUSTOMER

ROOT = Path(__file__).resolve().parent.parent.parent


def _script(name: str) -> ModuleType:
    sys.path.insert(0, str(ROOT / "scripts"))
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def case(transaction_id: str, category: str = "unrecognized_t1", **extra: Any) -> RealScenario:
    reference = CATEGORY[category].reference
    dispute: dict[str, Any] = {"reason_code": "RC_UNRECOGNIZED"}
    if category.startswith("unrecognized"):
        dispute |= {"card_in_possession": True, "shared_credentials": False, "block": "declined"}
    text = "{reference}" if reference == "id" else "{transaction}"
    return RealScenario.model_validate(
        {
            "id": "R001",
            "title": category,
            "category": category,
            "language": "es",
            "path": CATEGORY[category].path.value,
            "data_source": "real",
            "customer_id": CUSTOMER,
            "transaction_id": transaction_id,
            "reference": reference,
            "dispute": dispute,
            "script": {"first": f"Hola, es {text}", "answers": {"transaction_ref": f"Fue {text}"}},
            **extra,
        }
    )


def _old_purchase(db: Session) -> None:
    db.add(
        Transaction(
            transaction_id="TRX-OLD-PURCHASE",
            customer_id=CUSTOMER,
            product_id=CARD,
            transaction_date=datetime(2026, 1, 10, 12, 0),
            process_date=datetime(2026, 1, 10).date(),
            transaction_type="Purchase",
            amount=Decimal("40.00"),
            currency="USD",
            amount_usd=Decimal("40.00"),
            amount_usd_source="identity",
            merchant_name="Cafe Sintetico",
            transaction_status="Approved",
            fraud_score=Decimal("5"),
            source_file="test",
            ingested_at=datetime(2026, 10, 2, 9, 0),
        )
    )
    db.flush()


# --- The engine on the records -----------------------------------------------------------------


def test_an_unrecognized_purchase_resolves_on_its_records(
    loaded: Pipeline, db_session: Session
) -> None:
    scenario = case("TRX-T1-PURCHASE")
    records = read_records(db_session, scenario)
    assert records.transaction is not None and not records.foreign
    assert records.customer is not None and records.products
    label = label_real(scenario, records)
    assert (label.outcome.value, label.disputes[0].tier) == ("RESOLVE", "T1")
    assert label.disputes[0].actions == ["ACT-02", "ACT-04"]  # the block was declined
    assert label.disputes[0].transaction == "TRX-T1-PURCHASE"


@pytest.mark.parametrize(
    ("transaction_id", "category", "outcome", "reason"),
    [
        ("TRX-PENDING", "pending", "INFORM", "transaction_pending"),
        ("TRX-REVERSED", "reversed", "INFORM", "transaction_reversed"),
        ("TRX-OTHER-CUSTOMER", "another_customer", "REFUSE", None),
    ],
)
def test_the_label_comes_from_the_records(
    loaded: Pipeline,
    db_session: Session,
    transaction_id: str,
    category: str,
    outcome: str,
    reason: str | None,
) -> None:
    scenario = case(transaction_id, category)
    records = read_records(db_session, scenario)
    label = label_real(scenario, records)
    assert (label.outcome.value, label.disputes[0].inform_reason) == (outcome, reason)
    assert records.foreign is (category == "another_customer")
    if records.foreign:
        assert label.disputes[0].failed_gates == ["GATE-04"]


def test_a_reference_reads_a_record_outside_the_pool(loaded: Pipeline, db_session: Session) -> None:
    _old_purchase(db_session)
    described = read_records(db_session, case("TRX-OLD-PURCHASE"))
    assert all(t.transaction_id != "TRX-OLD-PURCHASE" for t in described.candidates)
    scenario = case("TRX-OLD-PURCHASE", "outside_window")
    records = read_records(db_session, scenario)
    assert any(t.transaction_id == "TRX-OLD-PURCHASE" for t in records.candidates)
    label = label_real(scenario, records)
    assert (label.outcome.value, label.disputes[0].inform_reason) == ("INFORM", "outside_window")


def test_a_missing_transaction_is_an_error(loaded: Pipeline, db_session: Session) -> None:
    with pytest.raises(LookupError, match="not found"):
        read_records(db_session, case("TRX-NOWHERE"))


def test_the_description_identifies_one_record(loaded: Pipeline, db_session: Session) -> None:
    records = read_records(db_session, case("TRX-T1-PURCHASE"))
    by_id = {t.transaction_id: t for t in records.candidates}
    assert description_identifies(by_id["TRX-T1-PURCHASE"], records.candidates)
    # Same merchant and amount, one business day: the description finds both.
    assert not description_identifies(by_id["TRX-DUP-SECOND"], records.candidates)


# --- Run time: the script and the document -----------------------------------------------------


def test_the_script_is_filled_from_the_record(loaded: Pipeline, db_session: Session) -> None:
    scenario = case("TRX-T1-PURCHASE")
    record = read_records(db_session, scenario).transaction
    assert record is not None
    script = render_script(scenario, record)
    assert script.first == "Hola, es una compra de 50 dólares en Cafe Sintetico el 16 de junio"
    assert script.answers["transaction_ref"].startswith("Fue una compra")
    with pytest.raises(ValueError, match="needs its record"):
        render_script(scenario, None)
    by_reference = render_script(case("TRX-OTHER-CUSTOMER", "another_customer"), None)
    assert by_reference.first == "Hola, es TRX-OTHER-CUSTOMER"


def test_descriptions_in_both_languages(loaded: Pipeline, db_session: Session) -> None:
    records = read_records(db_session, case("TRX-T1-PURCHASE"))
    fee = next(t for t in records.candidates if t.transaction_id == "TRX-FEE")
    as_of = label_as_of()
    assert describe_record(fee, "pt", as_of) == "um ajuste de 120.000 pesos dia 16 de junho"
    assert spoken_amount(Decimal("19011.31"), "COP") == "19.011,31 pesos"
    assert spoken_amount(Decimal("45.20"), "USD") == "45,20 dólares"


def test_the_document_is_read_from_the_raw_file(tmp_path: Path) -> None:
    raw = tmp_path / "customers.csv"
    raw.write_text(
        "customer_id,document_number,first_name\nCLI-A,1001,Ana\nCLI-B,1002,Beto\n",
        encoding="utf-8-sig",  # the supplied file starts with a BOM
    )
    assert document_for("CLI-B", raw) == "1002"
    assert document_for("CLI-Z", raw) is None
    assert document_for("CLI-A", tmp_path / "missing.csv") is None


def test_a_real_case_holds_identifiers_only() -> None:
    base = case("TRX-T1-PURCHASE").model_dump(mode="json")
    for broken in (
        {**base, "document_number": "1001"},
        {**base, "customer_id": "SEED-C001"},
        {**base, "data_source": "seeded"},
        {**base, "id": "S001"},
        {**base, "amount": "50.00"},
    ):
        with pytest.raises(ValidationError):
            RealScenario.model_validate(broken)


# --- The selection script ----------------------------------------------------------------------


def test_the_selection_queries_run_read_only(loaded: Pipeline, connection: Connection) -> None:
    selector = _script("select_real_scenarios")
    # The synthetic data cannot fill every category: the queries run and the selection stops.
    with pytest.raises(SystemExit, match="records meet the criteria"):
        selector.select(connection)


def test_a_label_other_than_intended_fails_the_selection(
    loaded: Pipeline, db_session: Session
) -> None:
    selector = _script("select_real_scenarios")
    scenario = case("TRX-PENDING", "unrecognized_t1")  # a pending purchase never resolves
    label = label_real(scenario, read_records(db_session, scenario))
    with pytest.raises(SystemExit, match="intended"):
        selector.check_label(scenario, label)
    ok = case("TRX-PENDING", "pending")
    selector.check_label(ok, label_real(ok, read_records(db_session, ok)))


def test_the_specification_of_a_selected_record() -> None:
    import random

    selector = _script("select_real_scenarios")
    picked = {"category": "another_customer", "language": "pt",
              "customer_id": "CLI-A", "transaction_id": "TRX-B"}  # fmt: skip
    spec = selector.specification(7, picked, random.Random(1))
    assert (spec.id, spec.reference, spec.data_source) == ("R007", "id", "real")
    assert (
        "{reference}" in spec.script.first
        or "{reference}" in spec.script.answers["transaction_ref"]
    )
    resolve = selector.specification(8, {**picked, "category": "unrecognized_t1"}, random.Random(1))
    assert resolve.dispute.block == "declined" and resolve.dispute.card_in_possession is True


def test_loading_the_real_files(tmp_path: Path) -> None:
    assert load_real_scenarios(tmp_path / "none.yaml") == []
    assert load_real_labels(tmp_path / "none.yaml") == []
    entry = case("TRX-T1-PURCHASE").model_dump(mode="json")
    twice = tmp_path / "real_cases.yaml"
    twice.write_text(yaml.safe_dump({"cases": [entry, entry]}), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate real scenario ids"):
        load_real_scenarios(twice)
    once = tmp_path / "one.yaml"
    once.write_text(yaml.safe_dump({"cases": [entry]}), encoding="utf-8")
    assert [c.transaction_id for c in load_real_scenarios(once)] == ["TRX-T1-PURCHASE"]
    empty = tmp_path / "labels.yaml"
    empty.write_text(yaml.safe_dump({"labels": []}), encoding="utf-8")
    assert load_real_labels(empty) == []


# --- The lock: what the repository keeps of the selection ---------------------------------------


def test_the_digest_pins_the_selection_without_naming_it() -> None:
    first = case("TRX-T1-PURCHASE")
    second = case("TRX-PENDING", "pending").model_copy(update={"id": "R002"})
    digest = selection_digest([first, second])
    assert digest == selection_digest([second, first])  # ordered by case key
    assert digest != selection_digest(
        [first, second.model_copy(update={"transaction_id": "TRX-X"})]
    )
    assert "TRX" not in digest and len(digest) == 64


def test_the_lock_round_trip_and_its_differences(
    loaded: Pipeline, db_session: Session, tmp_path: Path
) -> None:
    selector = _script("select_real_scenarios")
    scenarios = [
        case("TRX-T1-PURCHASE"),
        case("TRX-PENDING", "pending").model_copy(update={"id": "R002"}),
    ]
    labels = [label_real(s, read_records(db_session, s)) for s in scenarios]
    locked = selector.lock(scenarios, labels)
    assert all(d.transaction is None for c in locked.cases for d in c.expected.disputes)
    text = selector.lock_text(locked)
    assert "CLI-" not in text and "TRX-" not in text  # no identifier in the versioned file
    path = tmp_path / "real_selection.lock"
    path.write_text(text, encoding="utf-8")
    assert load_lock(path) == locked
    assert load_lock(tmp_path / "missing.lock") is None
    assert selector.differences(locked, locked) == []
    moved = selector.lock(
        [scenarios[0], scenarios[1].model_copy(update={"transaction_id": "TRX-T3-PURCHASE"})],
        [labels[0], without_records(labels[0]).model_copy(update={"case_id": "R002"})],
    )
    changed = selector.differences(locked, moved)
    assert "the selected identifiers changed (ids_sha256)" in changed
    assert "R002: the case or its expected label changed" in changed
    assert not any("TRX-" in line for line in changed)
