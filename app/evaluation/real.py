"""Scenarios anchored in real records of the supplied data (M17, policy §16).

A ``RealScenario`` names a real customer and a real transaction; nothing else about the
records is stored. Its records are read at run time, read-only, the way the Tool Layer reads
them (the customer, their products and cases, and the transactions within
``LATE_WINDOW_DAYS`` plus the one the customer names by its reference), and its label is the
Deterministic Policy Engine on those records. Real rows are never written.

The script holds placeholders filled at run time from the record: ``{transaction}`` (how the
customer describes it: kind, amount, merchant and business day) and ``{reference}`` (its
reference on the statement). The document number used to log in is read from
``data/raw/customers.csv`` by ``customer_id`` when it is needed and is never stored.

The cases are selected by ``scripts/select_real_scenarios.py`` with deterministic criteria and
a fixed seed. The identifiers stay out of the repository (the dataset is privately distributed
and no versioned file holds a customer or transaction identifier): the cases and their labels
are written to ``config/eval_scenarios/local/`` (git-ignored). The repository keeps
``config/eval_scenarios/real_selection.lock``: the criteria version, the seed, the count per
category, the expected label of each case key (``R001``...) and a SHA-256 of the selected
identifiers. The split, the mix and the review sample use the case keys and the lock only.
"""

from __future__ import annotations

import csv
import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from app.config import PolicyConfig, load_merchant_categories, load_policy_config
from app.contracts import (
    CaseRecord,
    Confirmation,
    CustomerRecord,
    Outcome,
    ProductRecord,
    ReasonCode,
    Slots,
    TransactionRecord,
    TransactionRef,
)
from app.evaluation.labeler import (
    LABEL_BUSINESS_DATE,
    CaseLabel,
    build_request,
    dispute_label,
)
from app.evaluation.scenarios import DEFAULT_SCENARIOS_DIR, Conditions, Path3, ScriptSpec
from app.policy import DeterministicPolicyEngine
from app.policy.clock import business_day
from app.policy.matching import Tolerance, match_transaction
from app.storage.models import Transaction
from app.storage.repositories import CoreBankingRepository

ROOT = Path(__file__).resolve().parent.parent.parent
LOCAL_DIR = DEFAULT_SCENARIOS_DIR / "local"  # git-ignored: real identifiers
REAL_CASES_FILE = LOCAL_DIR / "real_cases.yaml"
REAL_LABELS_FILE = LOCAL_DIR / "real_labels.yaml"
LOCK_FILE = DEFAULT_SCENARIOS_DIR / "real_selection.lock"
RAW_CUSTOMERS = ROOT / "data" / "raw" / "customers.csv"
RealId = Annotated[str, Field(pattern=r"^[A-Z]{3}-[A-Za-z0-9-]+$")]  # never SEED-


@dataclass(frozen=True)
class Category:
    """A selection criterion and the label it is meant to give (checked on selection)."""

    name: str
    path: Path3
    outcome: Outcome
    rules: tuple[str, ...] = ()
    tier: str | None = None
    inform_reason: str | None = None
    reference: Literal["description", "id"] = "description"


CATEGORIES: tuple[Category, ...] = (
    Category("unrecognized_t1", Path3.RESOLUTION, Outcome.RESOLVE, tier="T1"),
    Category("unrecognized_t2", Path3.RESOLUTION, Outcome.RESOLVE, tier="T2"),
    Category(
        "pending",
        Path3.AMBIGUOUS_OR_UNSUPPORTED,
        Outcome.INFORM,
        inform_reason="transaction_pending",
    ),
    Category(
        "declined",
        Path3.AMBIGUOUS_OR_UNSUPPORTED,
        Outcome.INFORM,
        inform_reason="transaction_declined",
    ),
    Category(
        "reversed",
        Path3.AMBIGUOUS_OR_UNSUPPORTED,
        Outcome.INFORM,
        inform_reason="transaction_reversed",
    ),
    Category(
        "outside_window",
        Path3.AMBIGUOUS_OR_UNSUPPORTED,
        Outcome.INFORM,
        inform_reason="outside_window",
        reference="id",
    ),
    Category("late_filing", Path3.HUMAN, Outcome.ESCALATE, rules=("ESC-07",)),
    Category("another_customer", Path3.AMBIGUOUS_OR_UNSUPPORTED, Outcome.REFUSE, reference="id"),
    Category(
        "not_disputable",
        Path3.AMBIGUOUS_OR_UNSUPPORTED,
        Outcome.INFORM,
        inform_reason="not_disputable",
    ),
)
CATEGORY = {category.name: category for category in CATEGORIES}


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


CategoryName = Literal[
    "unrecognized_t1",
    "unrecognized_t2",
    "pending",
    "declined",
    "reversed",
    "outside_window",
    "late_filing",
    "another_customer",
    "not_disputable",
]


class RealDispute(_Spec):
    """The final values of the slots the customer gives (the transaction is the case's)."""

    reason_code: ReasonCode
    card_in_possession: bool | None = None
    shared_credentials: bool | None = None
    block: Literal["confirmed", "declined"] | None = None
    confirmation: Literal["confirmed", "withdrawn"] = "confirmed"


class RealScenario(_Spec):
    id: Annotated[str, Field(pattern=r"^R\d{3}$")]
    title: str
    category: CategoryName
    language: Literal["es", "pt"]
    path: Path3
    data_source: Literal["real"]
    customer_id: RealId  # the customer who logs in
    transaction_id: RealId  # the disputed transaction (another customer's: GATE-04)
    reference: Literal["description", "id"]  # how the script names the transaction
    dispute: RealDispute
    script: ScriptSpec

    @property
    def conditions(self) -> Conditions:
        return Conditions()  # authenticated, nothing special: the records carry the case


class LockedCase(_Spec):
    """A real case as the repository knows it: its key, category, language, path and the
    label it is expected to get, without any identifier."""

    id: Annotated[str, Field(pattern=r"^R\d{3}$")]
    category: CategoryName
    language: Literal["es", "pt"]
    path: Path3
    data_source: Literal["real"] = "real"
    expected: CaseLabel  # its transactions are null: the lock names no record

    @property
    def title(self) -> str:
        return self.category

    @property
    def conditions(self) -> Conditions:
        return Conditions()


class SelectionLock(_Spec):
    """``config/eval_scenarios/real_selection.lock``."""

    criteria_version: str
    seed: str
    business_date: date
    counts: dict[CategoryName, dict[Literal["es", "pt"], int]]
    ids_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    cases: list[LockedCase]


def selection_digest(scenarios: list[RealScenario]) -> str:
    """SHA-256 of the selected identifiers, one line per case ordered by key:
    ``R001 <customer_id> <transaction_id>``. It pins the selection without naming a record."""
    ordered = sorted(scenarios, key=lambda s: s.id)
    lines = [f"{s.id} {s.customer_id} {s.transaction_id}" for s in ordered]
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def without_records(label: CaseLabel) -> CaseLabel:
    """The label with its transaction identifiers removed, as the lock keeps it."""
    disputes = [d.model_copy(update={"transaction": None}) for d in label.disputes]
    return label.model_copy(update={"disputes": disputes})


def load_lock(path: Path | None = None) -> SelectionLock | None:
    source = path or LOCK_FILE
    if not source.exists():
        return None
    return SelectionLock.model_validate(yaml.safe_load(source.read_text(encoding="utf-8")))


def load_real_scenarios(path: Path | None = None) -> list[RealScenario]:
    source = path or REAL_CASES_FILE
    if not source.exists():
        return []
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    cases = [RealScenario.model_validate(entry) for entry in raw.get("cases", [])]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate real scenario ids")
    return sorted(cases, key=lambda case: case.id)


def load_real_labels(path: Path | None = None) -> list[CaseLabel]:
    source = path or REAL_LABELS_FILE
    if not source.exists():
        return []
    raw = yaml.safe_load(source.read_text(encoding="utf-8")) or {}
    return [CaseLabel.model_validate(entry) for entry in raw.get("labels", [])]


# --- Records, read-only -------------------------------------------------------------------------


@dataclass(frozen=True)
class RealRecords:
    """What the Tool Layer would read for the case."""

    customer: CustomerRecord | None
    products: list[ProductRecord]
    candidates: list[TransactionRecord]
    cases: list[CaseRecord]
    transaction: TransactionRecord | None  # the disputed one, when it is the customer's
    foreign: bool  # the reference names another customer's transaction (GATE-04)


def label_as_of(business_date: date = LABEL_BUSINESS_DATE) -> datetime:
    return datetime.combine(business_date + timedelta(days=1), time(6, 0))


def read_records(
    session: Session,
    scenario: RealScenario,
    config: PolicyConfig | None = None,
    business_date: date = LABEL_BUSINESS_DATE,
) -> RealRecords:
    """The records of a real case, with the candidate pool of ``transaction_candidates``.
    Only reads; the caller opens the transaction read-only."""
    p = (config or load_policy_config()).parameters
    repo = CoreBankingRepository(session)
    as_of = label_as_of(business_date)
    customer_id = scenario.customer_id
    pool = repo.find_transactions(
        customer_id, start=as_of - timedelta(days=p.LATE_WINDOW_DAYS + 1), end=as_of
    )
    own = repo.get_transaction(customer_id, scenario.transaction_id)
    by_id = scenario.reference == "id"
    if own is not None and by_id and all(t.transaction_id != own.transaction_id for t in pool):
        pool.append(own)  # the customer gave the reference: the Tool Layer reads it too
    if own is None and session.get(Transaction, scenario.transaction_id) is None:
        raise LookupError(f"{scenario.id}: transaction {scenario.transaction_id} not found")
    return RealRecords(
        customer=repo.get_customer(customer_id),
        products=repo.list_products(customer_id),
        candidates=pool,
        cases=repo.list_cases(customer_id),
        transaction=own,
        foreign=own is None,
    )


def label_real(
    scenario: RealScenario,
    records: RealRecords,
    config: PolicyConfig | None = None,
    business_date: date = LABEL_BUSINESS_DATE,
) -> CaseLabel:
    """The engine on the real records (policy §16.2), with the specified final slots."""
    config = config or load_policy_config()
    dispute = scenario.dispute
    slots = Slots(
        transaction_ref=TransactionRef(transaction_id=scenario.transaction_id),
        reason_code=dispute.reason_code,
        card_in_possession=dispute.card_in_possession,
        shared_credentials=dispute.shared_credentials,
        confirmation=(
            Confirmation.WITHDRAWN
            if dispute.confirmation == "withdrawn"
            else Confirmation.CONFIRMED
        ),
    )
    request = build_request(
        case_id=scenario.id,
        language=scenario.language,
        conditions=scenario.conditions,
        slots=slots,
        config=config,
        as_of=label_as_of(business_date),
        unrecognized_before=0,
        customer=records.customer,
        candidates=list(records.candidates),
        products=list(records.products),
        cases=list(records.cases),
        foreign=records.foreign,
    )
    decision = DeterministicPolicyEngine(config).evaluate(request)
    label = dispute_label(decision, scenario.transaction_id, dispute.block)
    return CaseLabel(
        case_id=scenario.id,
        language=scenario.language,
        path=scenario.path,
        outcome=label.outcome,
        triggered_rules=sorted(label.triggered_rules),
        disputes=[label],
    )


# --- Run time: the document and the script --------------------------------------------------------


def document_for(customer_id: str, path: Path | None = None) -> str | None:
    """The document number of a real customer, read from the raw file when it is needed (the
    database keeps only its HMAC). Never write it to a file or a report."""
    source = path or RAW_CUSTOMERS
    if not source.exists():
        return None
    with source.open(encoding="utf-8-sig", newline="") as handle:
        for row in csv.DictReader(handle):
            if row["customer_id"] == customer_id:
                return row["document_number"]
    return None


MONTHS = {
    "es": ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
           "septiembre", "octubre", "noviembre", "diciembre"],
    "pt": ["janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto",
           "setembro", "outubro", "novembro", "dezembro"],
}  # fmt: skip
KINDS = {
    "Purchase": ("una compra", "uma compra"),
    "Withdrawal": ("un retiro", "um saque"),
    "Transfer": ("una transferencia", "uma transferência"),
    "Payment": ("un pago", "um pagamento"),
    "Deposit": ("un depósito", "um depósito"),
    "Adjustment": ("un ajuste", "um ajuste"),
}


def spoken_amount(amount: Decimal, currency: str) -> str:
    """As a customer writes it: 45,20 dólares, 180.000 pesos (whole amounts without cents)."""
    whole, cents = f"{amount:.2f}".split(".")
    grouped = f"{int(whole):,}".replace(",", ".")
    number = grouped if cents == "00" else f"{grouped},{cents}"
    return f"{number} {'dólares' if currency == 'USD' else 'pesos'}"


def describe_record(record: TransactionRecord, language: str, as_of: datetime) -> str:
    """How the customer describes the transaction: kind, amount, merchant, business day."""
    es = language == "es"
    day = business_day(record.transaction_date, as_of)
    month = MONTHS["es" if es else "pt"][day.month - 1]
    kind = KINDS[record.transaction_type][0 if es else 1]
    where = f"{' en ' if es else ' em '}{record.merchant_name}" if record.merchant_name else ""
    when = f"el {day.day} de {month}" if es else f"dia {day.day} de {month}"
    return f"{kind} de {spoken_amount(record.amount, record.currency)}{where} {when}"


def described_ref(record: TransactionRecord, as_of: datetime) -> TransactionRef:
    """What the description of ``describe_record`` gives GATE-05: day, amount, merchant."""
    return TransactionRef(
        transaction_date=business_day(record.transaction_date, as_of),
        amount=record.amount,
        merchant=record.merchant_name,
    )


def description_identifies(
    record: TransactionRecord,
    pool: list[TransactionRecord],
    config: PolicyConfig | None = None,
    business_date: date = LABEL_BUSINESS_DATE,
) -> bool:
    """GATE-05 on the description finds exactly this record in the pool."""
    p = (config or load_policy_config()).parameters
    as_of = label_as_of(business_date)
    match = match_transaction(
        described_ref(record, as_of),
        pool,
        as_of,
        p.LATE_WINDOW_DAYS,
        Tolerance(Decimal(p.AMOUNT_TOLERANCE_PCT), p.AMOUNT_TOLERANCE_USD),
        load_merchant_categories(),
    )
    return (
        match.transaction is not None and match.transaction.transaction_id == record.transaction_id
    )


def render_script(
    scenario: RealScenario,
    record: TransactionRecord | None,
    business_date: date = LABEL_BUSINESS_DATE,
) -> ScriptSpec:
    """The script with its placeholders filled from the record (``record`` is needed only
    when the customer describes the transaction)."""
    values = {"reference": scenario.transaction_id}
    if scenario.reference == "description":
        if record is None:
            raise ValueError(f"{scenario.id}: describing the transaction needs its record")
        values["transaction"] = describe_record(
            record, scenario.language, label_as_of(business_date)
        )

    def fill(text: str) -> str:
        return text.format_map(values)

    script = scenario.script
    return ScriptSpec(
        first=fill(script.first),
        answers={kind: fill(text) for kind, text in script.answers.items()},
        side_questions=[fill(text) for text in script.side_questions],
    )


__all__ = [
    "CATEGORIES",
    "CATEGORY",
    "LOCK_FILE",
    "Category",
    "LockedCase",
    "RealDispute",
    "RealRecords",
    "RealScenario",
    "SelectionLock",
    "describe_record",
    "described_ref",
    "description_identifies",
    "document_for",
    "label_real",
    "load_lock",
    "load_real_labels",
    "load_real_scenarios",
    "read_records",
    "render_script",
    "selection_digest",
    "without_records",
]
