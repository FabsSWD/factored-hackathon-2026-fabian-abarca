"""The cases of the evaluation split, ready to be played (M18).

A seeded case comes from its specification (``SEED-`` records, document ``SEED-0007``). A real
case comes from the lock (its key, category and expected label) and from
``config/eval_scenarios/local/`` (its identifiers); its script is filled from the real record
and its document number is read from ``data/raw/customers.csv`` when the case is loaded. The
customer ID and the document live only in memory: every output names a case by its key.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.contracts import ReasonCode
from app.evaluation.harness.faults import FaultPlan
from app.evaluation.labeler import CaseLabel, label_all
from app.evaluation.real import (
    RealScenario,
    document_for,
    load_lock,
    load_real_scenarios,
    read_records,
    render_script,
)
from app.evaluation.scenarios import Conditions, Scenario, ScriptSpec, load_scenarios
from app.storage.models import Customer

# The order in which a conversation asks for things, for the baseline's transcript.
ANSWER_ORDER = (
    "language",
    "authentication",
    "ask_rephrase",
    "transaction_ref",
    "reason_code",
    "card_in_possession",
    "shared_credentials",
    "expected_amount",
    "expected_delivery_date",
    "merchant_contacted",
    "duplicate_ref",
    "fee_ref",
    "block_offer",
    "summary",
    "confirmation",
)
SEED_REFERENCE = re.compile(r"\bSEED-T\d{3}-[a-z0-9_]+\b")


class CaseLoadError(RuntimeError):
    """A case cannot be played: its document or its local identifiers are missing."""


@dataclass(frozen=True)
class Segment:
    """DATA-02 attributes, for the fairness breakdown only (never a decision input)."""

    country: str = "unknown"
    age_band: str = "unknown"
    gender: str = "unknown"


@dataclass(frozen=True)
class EvalCase:
    key: str
    title: str
    language: str
    path: str
    data_source: str  # "seeded" | "real"
    label: CaseLabel
    script: ScriptSpec
    conditions: Conditions
    reasons: tuple[ReasonCode | None, ...]  # the reason of each dispute, in order
    # In memory only, never written to an output.
    customer_id: str = field(repr=False)
    document: str | None = field(repr=False)
    references: tuple[str, ...] = field(default=(), repr=False)  # transactions named by reference
    # The transaction each dispute is about, to pick it when candidates are shown.
    targets: tuple[str | None, ...] = field(default=(), repr=False)
    segment: Segment = Segment()

    @property
    def faults(self) -> FaultPlan:
        return FaultPlan(
            tool_write_failure=self.conditions.tool_failure,
            models_down=self.conditions.models_unavailable,
        )

    @property
    def logs_in(self) -> bool:
        return self.conditions.authenticated

    def transcript(self) -> list[str]:
        """The customer's messages in the order a conversation asks for them (baseline)."""
        answers = self.script.answers
        lines = [self.script.first, *self.script.side_questions]
        lines += [answers[key] for key in ANSWER_ORDER if key in answers]
        lines += [answers[key] for key in sorted(answers) if key.startswith("transaction_ref_")]
        return lines


def evaluation_keys(split_evaluation: Sequence[str]) -> list[str]:
    return sorted(split_evaluation)


def seeded_cases(keys: set[str], scenarios: list[Scenario] | None = None) -> list[EvalCase]:
    chosen = [s for s in (scenarios or load_scenarios()) if s.id in keys]
    labels = {label.case_id: label for label in label_all(chosen)}
    cases = []
    for s in chosen:
        text = " ".join([s.script.first, *s.script.answers.values()])
        cases.append(
            EvalCase(
                key=s.id,
                title=s.title,
                language=s.language,
                path=s.path.value,
                data_source="seeded",
                label=labels[s.id],
                script=s.script,
                conditions=s.conditions,
                reasons=tuple(d.reason_code for d in s.disputes),
                customer_id=s.customer_id,
                document=s.document_number,
                references=tuple(sorted(set(SEED_REFERENCE.findall(text)))),
                targets=tuple(
                    s.transaction_id(d.transaction) if d.transaction else None for d in s.disputes
                ),
            )
        )
    return cases


def real_cases(
    keys: set[str],
    session_factory: sessionmaker[Session],
    documents: Callable[[str], str | None] = document_for,
    local: list[RealScenario] | None = None,
) -> list[EvalCase]:
    """The real cases of ``keys``: identifiers from local/, labels from the lock, scripts from
    the records (read-only), documents from the raw file."""
    lock = load_lock()
    if lock is None:
        raise CaseLoadError("real_selection.lock is missing")
    expected = {c.id: c for c in lock.cases if c.id in keys}
    if not expected:
        return []
    found = {c.id: c for c in (local if local is not None else load_real_scenarios())}
    missing = sorted(set(expected) - set(found))
    if missing:
        raise CaseLoadError(
            f"{missing} are not in config/eval_scenarios/local/: run "
            "scripts/select_real_scenarios.py"
        )
    cases = []
    with session_factory() as session:
        for key in sorted(expected):
            scenario = found[key]
            document = documents(scenario.customer_id)
            if document is None:
                raise CaseLoadError(f"{key}: the customer is not in data/raw/customers.csv")
            records = read_records(session, scenario)
            cases.append(
                EvalCase(
                    key=key,
                    title=scenario.title,
                    language=scenario.language,
                    path=scenario.path.value,
                    data_source="real",
                    label=expected[key].expected,
                    script=render_script(scenario, records.transaction),
                    conditions=scenario.conditions,
                    reasons=(scenario.dispute.reason_code,),
                    customer_id=scenario.customer_id,
                    document=document,
                    references=(scenario.transaction_id,) if scenario.reference == "id" else (),
                    targets=(scenario.transaction_id,),
                )
            )
        session.rollback()
    return cases


def with_segments(cases: list[EvalCase], session_factory: sessionmaker[Session]) -> list[EvalCase]:
    """Each case with its customer's country, age band and gender, read from the database."""
    ids = {c.customer_id for c in cases}
    with session_factory() as session:
        rows = session.execute(
            select(
                Customer.customer_id, Customer.country, Customer.age_band, Customer.gender
            ).where(Customer.customer_id.in_(ids))
        ).all()
        session.rollback()
    found = {
        cid: Segment(country or "unknown", band or "unknown", gender or "unknown")
        for cid, country, band, gender in rows
    }
    return [replace(c, segment=found.get(c.customer_id, Segment())) for c in cases]
