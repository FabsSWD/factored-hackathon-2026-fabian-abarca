"""The baseline (M18): GPT-6 Luna deciding alone, without the Policy Engine.

One call per case. The model receives the policy (``docs/dispute-policy.md``), the customer's
records minimized exactly as the system sends them to the model (DATA-01: a pseudonymous
reference, masked product numbers, the transactions in the late window with date, amount,
currency, merchant and status; nothing before authentication), each transaction's USD amount
(baseline@1.1.0: without it the model took a peso amount for a T3), whether the customer
authenticated, and the customer's whole scripted conversation. It predicts the outcome, the
triggered rules, the queue and the priority. It sees neither fraud scores nor product or
customer statuses nor earlier cases: DATA-01 keeps them from any model, and in the system the
engine reads them from the Tool Layer. That gap is part of what the comparison measures. It has
no model signals either, so ESC-11 does not apply to it; the cases with injected faults
(ESC-10, ESC-11) are reported apart for the baseline.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy.orm import Session, sessionmaker

from app.contracts import LLMContext, LLMTransaction, TransactionRecord
from app.deadline import Deadline
from app.evaluation.harness.cases import EvalCase
from app.evaluation.harness.metrics import Prediction
from app.llm_adapter.client import JsonCompletion
from app.policy.clock import business_date
from app.pseudonym import UNAUTHENTICATED_REF, customer_ref
from app.storage.repositories import CoreBankingRepository

BASELINE_PROMPT_VERSION = "baseline@1.1.0"
BASELINE_PURPOSE = "baseline_decision"
BASELINE_MAX_TOKENS = 2000
POLICY_FILE = Path(__file__).resolve().parents[3] / "docs" / "dispute-policy.md"
RULES = [f"ESC-{n:02d}" for n in range(1, 15)]
# The model sees transaction references (DATA-01 allows them); its words never carry them into
# a report, where no customer, product or transaction identifier may appear.
IDENTIFIER = re.compile(r"\b(?:CLI|PRD|TRX|CMP)-[A-Za-z0-9-]{6,}\b|\b[0-9a-f]{64}\b")


def scrub(text: str) -> str:
    return IDENTIFIER.sub("[ref]", text)


BASELINE_INSTRUCTIONS = """\
You decide the outcome of a bank customer's transaction-dispute conversation by applying the \
dispute policy below. There is no other system: your answer is the decision.

You receive JSON with: the conversation language; whether the customer authenticated; the \
customer's records as the bank's assistant may see them (a pseudonymous reference, masked \
product numbers, recent transactions with their USD amount in amount_usd, which decides the \
tier); and the customer's messages in order (the assistant's questions are not included; \
assume each message answers the natural next question).

You have no model signals: ESC-11 (uncertain or unavailable models) never applies to you.

Return the final outcome of the conversation (RESOLVE, INFORM, REFUSE or ESCALATE; CLARIFY \
only if the conversation ends while information is still missing), the escalation triggers \
that fire (ESC-xx identifiers, empty unless the outcome is ESCALATE), and the queue and \
priority of the escalation (null unless ESCALATE). Keep the rationale under 300 characters.

# Dispute policy
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "outcome": {
            "type": "string",
            "enum": ["RESOLVE", "INFORM", "REFUSE", "ESCALATE", "CLARIFY"],
        },
        "triggered_rules": {"type": "array", "items": {"type": "string", "enum": RULES}},
        "queue": {
            "type": ["string", "null"],
            "enum": ["disputes", "fraud", "security_review", None],
        },
        "priority": {"type": ["string", "null"], "enum": ["normal", "high", None]},
        "rationale": {"type": "string"},
    },
    "required": ["outcome", "triggered_rules", "queue", "priority", "rationale"],
    "additionalProperties": False,
}


class JsonCompleter(Protocol):
    async def complete_json(
        self,
        *,
        purpose: str,
        prompt_version: str,
        system: str,
        user: str,
        schema_name: str,
        schema: dict[str, Any],
        max_output_tokens: int,
        validate: Callable[[dict[str, Any]], Any] | None = None,
        deadline: Deadline | None = None,
        max_retries: int | None = None,
    ) -> JsonCompletion: ...


def system_prompt(policy_text: str | None = None) -> str:
    text = policy_text if policy_text is not None else POLICY_FILE.read_text(encoding="utf-8")
    return BASELINE_INSTRUCTIONS + text


def validate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("outcome") not in SCHEMA["properties"]["outcome"]["enum"]:
        raise ValueError("baseline outcome outside the schema")
    rules = data.get("triggered_rules") or []
    if any(rule not in RULES for rule in rules):
        raise ValueError("baseline rule outside the schema")
    return data


@dataclass(frozen=True)
class BaselineRecords:
    """What the baseline sees: the DATA-01 context and the USD amount of each transaction."""

    context: LLMContext
    amount_usd: dict[str, Decimal | None] = field(default_factory=dict)


def context_for(
    case: EvalCase,
    session_factory: sessionmaker[Session],
    pseudonym_key: str,
    as_of: datetime,
    late_window_days: int,
) -> BaselineRecords:
    """DATA-01: what the system's model may see for this customer; nothing before login."""
    if not case.logs_in:
        anonymous = LLMContext(customer_ref=UNAUTHENTICATED_REF, business_date=business_date(as_of))
        return BaselineRecords(anonymous)
    with session_factory() as session:
        repo = CoreBankingRepository(session)
        products = repo.list_products(case.customer_id)
        pool: list[TransactionRecord] = repo.find_transactions(
            case.customer_id, start=as_of - timedelta(days=late_window_days + 1), end=as_of
        )
        for reference in case.references:  # named by its reference: the Tool Layer reads it
            if all(t.transaction_id != reference for t in pool):
                own = repo.get_transaction(case.customer_id, reference)
                if own is not None:
                    pool.append(own)
        session.rollback()
    context = LLMContext(
        customer_ref=customer_ref(case.customer_id, pseudonym_key),
        language=None,
        masked_products=[p.product_number_masked for p in products],
        transactions=[
            LLMTransaction(
                transaction_ref=t.transaction_id,
                transaction_date=t.transaction_date.date(),
                amount=t.amount,
                currency=t.currency,
                merchant_name=t.merchant_name,
                transaction_status=t.transaction_status,
            )
            for t in pool
        ],
        business_date=business_date(as_of),
    )
    return BaselineRecords(context, {t.transaction_id: t.amount_usd for t in pool})


def user_message(case: EvalCase, records: BaselineRecords) -> str:
    data = records.context.model_dump(mode="json", exclude={"pending_slot", "shown_candidates"})
    for txn in data["transactions"]:
        usd = records.amount_usd.get(txn["transaction_ref"])
        txn["amount_usd"] = None if usd is None else str(usd)
    return json.dumps(
        {
            "language": case.language,
            "authenticated": case.logs_in,
            "records": data,
            "customer_messages": case.transcript(),
        },
        ensure_ascii=False,
    )


class BaselinePredictor:
    def __init__(self, client: JsonCompleter, policy_text: str | None = None) -> None:
        self._client = client
        self._system = system_prompt(policy_text)

    async def predict(
        self, case: EvalCase, records: BaselineRecords, run: str = "baseline"
    ) -> tuple[Prediction, str]:
        """The prediction and the model's rationale. A failed call is a failed case."""
        started = time.perf_counter()
        try:
            answer = await self._client.complete_json(
                purpose=BASELINE_PURPOSE,
                prompt_version=BASELINE_PROMPT_VERSION,
                system=self._system,
                user=user_message(case, records),
                schema_name="dispute_decision",
                schema=SCHEMA,
                max_output_tokens=BASELINE_MAX_TOKENS,
                validate=validate,
            )
        except Exception as exc:  # recorded, never raised
            elapsed = (time.perf_counter() - started) * 1000
            return Prediction(case.key, run, None, conversation_ms=elapsed, end="error",
                              error=scrub(f"{type(exc).__name__}: {exc}")), ""  # fmt: skip
        data = answer.value
        elapsed = (time.perf_counter() - started) * 1000
        rules = tuple(sorted(set(data["triggered_rules"])))
        return (
            Prediction(
                key=case.key,
                run=run,
                outcome=data["outcome"],
                rules=rules,
                queue=data["queue"],
                priority=data["priority"],
                conversation_ms=elapsed,
                turn_ms=(elapsed,),
                end="final",
            ),
            scrub(str(data.get("rationale", "")))[:300],
        )
