"""Scenario specifications (policy §16.1), loaded and validated from config/eval_scenarios/.

A specification lists the records of one case (one customer, their products and transactions,
earlier cases), the customer's true intent (the disputes, in order, with their final slot
values), special conditions, and the script M18 plays: a first message and one prepared answer
per question the assistant may ask. Dates are relative to ``BUSINESS_DATE`` (``days_ago``), so
a specification means the same with any business date.

Identifiers are derived from the case number: customer ``SEED-C007``, product
``SEED-P007-card``, transaction ``SEED-T007-t1``, earlier case ``SEED-D007-1``, document number
``SEED-0007``. Nothing in a specification can name a real record: the cases anchored in real
records are ``RealScenario`` (``app/evaluation/real.py``), in a file of their own.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.contracts import CaseStatus, ClarifyTarget, ReasonCode
from app.storage.data_contract import (
    CURRENCIES,
    CUSTOMER_STATUSES,
    PRODUCT_STATUSES,
    PRODUCT_TYPES,
    TRANSACTION_STATUSES,
    TRANSACTION_TYPES,
)

DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent.parent.parent / "config" / "eval_scenarios"
SEED_PREFIX = "SEED-"
COUNTRIES = ("Colombia", "México", "Argentina")  # the countries of the supplied data
AGE_BANDS = ("18-24", "25-34", "35-44", "45-54", "55-64", "65+")
Key = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]{0,15}$")]


class Path3(StrEnum):
    """The three paths the challenge asks for."""

    RESOLUTION = "resolution"  # normal resolution
    AMBIGUOUS_OR_UNSUPPORTED = "ambiguous_or_unsupported"
    HUMAN = "human"  # needs human intervention


class _Spec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CustomerSpec(_Spec):
    status: Literal["Active", "Inactive", "Closed", "Suspended"] = "Active"
    country: Literal["Colombia", "México", "Argentina"]
    age_band: Literal["18-24", "25-34", "35-44", "45-54", "55-64", "65+"]
    gender: Literal["F", "M", "O"]
    segment: Literal["Basic", "Plus", "Premium", "Student"] = "Basic"


class ProductSpec(_Spec):
    key: Key
    type: str = "Tarjeta Crédito"
    currency: str = "USD"
    status: str = "Active"
    last4: Annotated[str, Field(pattern=r"^\d{4}$")]

    @model_validator(mode="after")
    def _known(self) -> Self:
        if self.type not in PRODUCT_TYPES or self.status not in PRODUCT_STATUSES:
            raise ValueError(f"unknown product type or status: {self.type}, {self.status}")
        if self.currency not in CURRENCIES:
            raise ValueError(f"currency {self.currency} is not in the data")
        return self


class TransactionSpec(_Spec):
    key: Key
    product: Key
    type: str = "Purchase"
    days_ago: Annotated[int, Field(ge=0, le=400)]
    at: time = time(14, 30)  # after the 06:00 cutoff: the business day is BUSINESS_DATE - days_ago
    amount: Annotated[Decimal, Field(gt=0)]
    currency: str = "USD"
    amount_usd: Decimal | None = None  # USD: the amount itself
    merchant: str | None = None
    category: str | None = None
    status: str = "Approved"
    fraud_score: Annotated[float, Field(ge=0, le=100)] | None = 12.0
    foreign: bool = False  # belongs to another (seeded) customer: GATE-04

    @model_validator(mode="after")
    def _known(self) -> Self:
        if self.type not in TRANSACTION_TYPES or self.status not in TRANSACTION_STATUSES:
            raise ValueError(f"unknown transaction type or status: {self.type}, {self.status}")
        if self.currency not in CURRENCIES:
            raise ValueError(f"currency {self.currency} is not in the data")
        if self.at < time(6, 0):
            raise ValueError("keep transactions after the 06:00 business-day cutoff")
        return self

    @property
    def usd(self) -> Decimal | None:
        return self.amount if self.currency == "USD" else self.amount_usd


class PriorCaseSpec(_Spec):
    transaction: Key
    reason_code: ReasonCode
    status: CaseStatus = CaseStatus.OPEN
    days_ago: Annotated[int, Field(ge=0, le=400)] = 5


class DisputeSpec(_Spec):
    """One transaction the customer disputes, with the final values of its slots."""

    transaction: Key | None = None  # None: the customer never identifies it (ESC-09)
    reason_code: ReasonCode | None = None
    card_in_possession: bool | None = None
    shared_credentials: bool | None = None
    expected_amount: Decimal | None = None
    delivery_days_ago: int | None = None  # expected delivery date, relative (negative: future)
    merchant_contacted: bool | None = None
    fee_ref: str | None = None
    duplicate: Key | None = None  # the twin the customer confirms (RC_DUPLICATE)
    block: Literal["confirmed", "declined"] | None = None  # answer to the ACT-03 offer
    confirmation: Literal["confirmed", "withdrawn"] = "confirmed"  # final answer to the summary


class Conditions(_Spec):
    """Special conditions of the case (policy §16.1)."""

    authenticated: bool = True
    authentication_declined: bool = False
    authentication_attempts_exhausted: bool = False
    detected_language: str | None = None  # a language other than es/pt: GATE-01
    injection_strikes: Annotated[int, Field(ge=0)] = 0
    human_requested: bool = False
    legal_or_vulnerability: bool = False
    account_takeover_reported: bool = False
    expired_session: bool = False  # the session expires mid-way and the customer logs in again
    hedged_confirmation: bool = False  # an unclear yes first, then a clear one
    models_unavailable: bool = False  # extract and Kev both down: ESC-11
    tool_failure: bool = False  # ACT-02 fails after its retries: ESC-10
    unresolved: ClarifyTarget | None = None  # never answered: ESC-09 on this target
    # Distinct unrecognized charges the customer reports in one message (policy §7, v0.4.11).
    unrecognized_reported: Annotated[int, Field(ge=0)] = 0


class ScriptSpec(_Spec):
    """What the customer writes. ``answers`` are keyed by what the assistant asks: a CLARIFY
    target (``transaction_ref``, ``reason_code``, ...), ``block_offer``, ``summary``,
    ``authentication``, ``language``; M18 picks the answer from the trace's reply kind."""

    first: str
    answers: dict[str, str] = Field(default_factory=dict)
    side_questions: list[str] = Field(default_factory=list)  # asked along the way


class Scenario(_Spec):
    id: Annotated[str, Field(pattern=r"^S\d{3}$")]
    title: str
    language: Literal["es", "pt", "en"]
    path: Path3
    data_source: Literal["seeded"]  # real cases are RealScenario (app/evaluation/real.py)
    notes: str | None = None
    customer: CustomerSpec
    products: list[ProductSpec]
    transactions: list[TransactionSpec] = Field(default_factory=list)
    prior_cases: list[PriorCaseSpec] = Field(default_factory=list)
    disputes: list[DisputeSpec]
    conditions: Conditions = Field(default_factory=Conditions)
    script: ScriptSpec

    @model_validator(mode="after")
    def _references(self) -> Self:
        products = {p.key for p in self.products}
        transactions = {t.key for t in self.transactions}
        if len(products) != len(self.products) or len(transactions) != len(self.transactions):
            raise ValueError(f"{self.id}: duplicate product or transaction key")
        for txn in self.transactions:
            if txn.product not in products:
                raise ValueError(f"{self.id}: {txn.key} names an unknown product {txn.product}")
        for prior in self.prior_cases:
            if prior.transaction not in transactions:
                raise ValueError(f"{self.id}: an earlier case names {prior.transaction}")
        for dispute in self.disputes:
            for key in (dispute.transaction, dispute.duplicate):
                if key is not None and key not in transactions:
                    raise ValueError(f"{self.id}: a dispute names an unknown transaction {key}")
        if not self.disputes:
            raise ValueError(f"{self.id}: a case disputes at least one transaction")
        return self

    # --- identifiers --------------------------------------------------------------------

    @property
    def number(self) -> int:
        return int(self.id[1:])

    @property
    def customer_id(self) -> str:
        return f"{SEED_PREFIX}C{self.number:03d}"

    @property
    def foreign_customer_id(self) -> str:
        return f"{SEED_PREFIX}C{self.number:03d}X"

    @property
    def document_number(self) -> str:
        return f"{SEED_PREFIX}{self.number:04d}"

    def product_id(self, key: str) -> str:
        return f"{SEED_PREFIX}P{self.number:03d}-{key}"

    def transaction_id(self, key: str) -> str:
        return f"{SEED_PREFIX}T{self.number:03d}-{key}"

    def case_id(self, index: int) -> str:
        return f"{SEED_PREFIX}D{self.number:03d}-{index}"

    def transaction(self, key: str) -> TransactionSpec:
        return next(t for t in self.transactions if t.key == key)


def transaction_moment(spec: TransactionSpec, business_date: date) -> datetime:
    """The naive local timestamp of a transaction: its business day at ``at``."""
    return datetime.combine(business_date - timedelta(days=spec.days_ago), spec.at)


def load_scenarios(directory: Path | str | None = None) -> list[Scenario]:
    """Every case of every YAML file of the directory, ordered by id; ids are unique."""
    root = Path(directory) if directory is not None else DEFAULT_SCENARIOS_DIR
    cases: list[Scenario] = []
    for path in sorted(root.glob("cases_*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        cases += [Scenario.model_validate(entry) for entry in raw.get("cases", [])]
    ids = [case.id for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate scenario ids")
    return sorted(cases, key=lambda case: case.id)


def spec_version(directory: Path | str | None = None) -> str:
    """The version of the specifications (config/eval_scenarios/version.yaml)."""
    root = Path(directory) if directory is not None else DEFAULT_SCENARIOS_DIR
    raw = yaml.safe_load((root / "version.yaml").read_text(encoding="utf-8"))
    return str(raw["version"])


__all__ = [
    "AGE_BANDS",
    "COUNTRIES",
    "CUSTOMER_STATUSES",
    "Conditions",
    "CustomerSpec",
    "DisputeSpec",
    "Path3",
    "PriorCaseSpec",
    "ProductSpec",
    "Scenario",
    "ScriptSpec",
    "TransactionSpec",
    "load_scenarios",
    "spec_version",
    "transaction_moment",
]
