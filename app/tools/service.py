"""M9 Tool Layer: the only component that executes actions (architecture §3).

Bound at construction to the session's customer, so no method can be pointed at another
customer (GATE-04). Every read filters by that customer; a record of someone else and a record
that does not exist give the same ``AccessDeniedError`` (reads) or ``access_denied`` result
(actions), and both are recorded as a security event.

Writes follow policy §8: bounded retries on transient database errors (``TOOL_MAX_RETRIES``),
idempotency keys, and a read-back that must match before success is reported (COM-04). A
failure after the retries, or a read-back that does not match, is a ``failed`` result that the
Policy Engine escalates under ESC-10.

ACT-06 actions do not exist here, not even as guarded methods.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import InterfaceError, OperationalError
from sqlalchemy.orm import Session, sessionmaker
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_fixed

from app.config import PolicyParameters
from app.contracts import (
    AccessDeniedError,
    ActionId,
    CaseRecord,
    CaseStatus,
    CustomerRecord,
    HandoffPacket,
    ProductRecord,
    ProvisionalCreditFlag,
    ReasonCode,
    SessionContext,
    Tier,
    ToolResult,
    ToolStatus,
    TransactionRecord,
)
from app.storage.data_contract import CARD_PRODUCT_TYPES
from app.storage.models import CASE_NUMBER_SEQ, AuditLog, CardBlock, Case, HandoffPacketRow
from app.storage.repositories import CoreBankingRepository

TRANSIENT_ERRORS: tuple[type[Exception], ...] = (OperationalError, InterfaceError)
"""Database errors worth retrying: lost connections and timeouts, not constraint violations."""

EVENT_KIND = "access_denied"
BLOCKABLE_STATUSES = frozenset({"Active", "Blocked"})  # Blocked: repeating is idempotent

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ToolConfig:
    """``as_of`` is the business clock (policy §15): the pool window and
    ``business_created_at`` of new cases. ``retry_wait_seconds`` is the pause between
    retries."""

    as_of: datetime
    parameters: PolicyParameters
    retry_wait_seconds: float = 0.2


def tier_for(amount_usd: Decimal | None, parameters: PolicyParameters) -> Tier:
    """Policy §6, with ``>`` and ``<=`` as written; an unknown amount is T3."""
    if amount_usd is None or amount_usd > parameters.AUTO_INTAKE_MAX_USD:
        return Tier.T3
    if amount_usd > parameters.PROVISIONAL_CREDIT_AUTO_MAX_USD:
        return Tier.T2
    return Tier.T1


CREDIT_FLAGS = {
    Tier.T1: ProvisionalCreditFlag.ELIGIBLE,
    Tier.T2: ProvisionalCreditFlag.REQUIRES_REVIEW,
}


class DatabaseToolLayer:
    """Implements ``app.interfaces.ToolLayer`` over the Core Banking and Cases stores."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        session: SessionContext | None,
        config: ToolConfig,
        *,
        conversation_id: str | None = None,
        clock: Clock = utc_now,
    ) -> None:
        self._sessions = session_factory
        self._session = session
        self._config = config
        self._p = config.parameters
        self._conversation_id = conversation_id
        self._clock = clock

    @property
    def _customer_id(self) -> str | None:
        return self._session.customer_id if self._session is not None else None

    # ------------------------------------------------------------------ ACT-01 reads

    def get_customer(self) -> CustomerRecord:
        return self._read("customer", None, lambda repo, cid: repo.get_customer(cid))

    def list_products(self) -> list[ProductRecord]:
        return self._read("products", None, lambda repo, cid: repo.list_products(cid))

    def get_product(self, product_id: str) -> ProductRecord:
        return self._read(
            "product", product_id, lambda repo, cid: repo.get_product(cid, product_id)
        )

    def get_transaction(self, transaction_id: str) -> TransactionRecord:
        return self._read(
            "transaction",
            transaction_id,
            lambda repo, cid: repo.get_transaction(cid, transaction_id),
        )

    def transaction_candidates(self, transaction_id: str | None = None) -> list[TransactionRecord]:
        """The customer's transactions whose business day is within ``LATE_WINDOW_DAYS`` of
        ``BUSINESS_DATE``, newest first, plus ``transaction_id`` if the customer gave one."""
        as_of = self._config.as_of
        # Business day >= BUSINESS_DATE - LATE_WINDOW_DAYS, i.e. from that day at the cutoff.
        start = as_of - timedelta(days=self._p.LATE_WINDOW_DAYS + 1)
        pool = self._read(
            "transactions",
            None,
            lambda repo, cid: repo.find_transactions(cid, start=start, end=as_of),
        )
        if transaction_id is not None and all(t.transaction_id != transaction_id for t in pool):
            pool.append(self.get_transaction(transaction_id))
        return pool

    def get_case(self, case_id: str) -> CaseRecord:
        """A case by its reference. Case numbers are sequential, so the lookup is always
        filtered by the session customer: another customer's case is ``access_denied``."""
        return self._read("case", case_id, lambda repo, cid: repo.get_case(cid, case_id))

    def list_cases(self, transaction_id: str | None = None) -> list[CaseRecord]:
        if transaction_id is not None:
            self.get_transaction(transaction_id)  # ownership: AccessDeniedError otherwise
        return self._read("cases", None, lambda repo, cid: repo.list_cases(cid, transaction_id))

    def _read[T](
        self,
        record_type: str,
        record_id: str | None,
        query: Callable[[CoreBankingRepository, str], T | None],
    ) -> T:
        customer_id = self._customer_id
        if customer_id is None:
            self._security_event(record_type, record_id, reason="unauthenticated")
            raise AccessDeniedError
        for attempt in self._retrying():
            with attempt, self._sessions() as db:
                result = query(CoreBankingRepository(db), customer_id)
        if result is None:
            self._security_event(record_type, record_id)
            raise AccessDeniedError
        return result

    # ------------------------------------------------------------------ ACT-02 + ACT-04

    def create_case(self, transaction_id: str, reason_code: ReasonCode, tier: Tier) -> ToolResult:
        """ACT-02 with ACT-04. Idempotency key: ``transaction_id:reason_code``. The case
        reference is returned only after the read-back matches the transaction, reason code,
        tier and amount."""
        key = f"{transaction_id}:{reason_code.value}"
        customer_id = self._customer_id
        if customer_id is None:
            return self._denied(ActionId.CREATE_CASE, "transaction", transaction_id, key)

        def attempt(number: int) -> ToolResult:
            txn = self._own_transaction(customer_id, transaction_id)
            if txn is None:
                return self._denied(
                    ActionId.CREATE_CASE, "transaction", transaction_id, key, number
                )
            expected = tier_for(txn.amount_usd, self._p)
            if tier is Tier.T3 or tier is not expected:
                return self._failed(
                    ActionId.CREATE_CASE,
                    number,
                    f"tier {tier} does not match the amount (expected {expected})",
                    key,
                )
            assert txn.amount_usd is not None  # T1 and T2 have a known USD amount
            with self._sessions() as db:
                inserted = db.scalar(
                    insert(Case)
                    .values(
                        case_id=self._new_case_id(db),
                        customer_id=customer_id,
                        transaction_id=transaction_id,
                        reason_code=reason_code.value,
                        status=CaseStatus.OPEN.value,
                        tier=tier.value,
                        amount=txn.amount,
                        currency=txn.currency,
                        amount_usd=txn.amount_usd,
                        provisional_credit_flag=CREDIT_FLAGS[tier].value,
                        idempotency_key=key,
                        business_created_at=self._config.as_of,
                    )
                    .on_conflict_do_nothing(index_elements=[Case.idempotency_key])
                    .returning(Case.case_id)
                )
                db.commit()
            stored = self._read_back_case(customer_id, key)
            if stored is None or not _case_matches(stored, txn, reason_code, tier):
                return self._failed(ActionId.CREATE_CASE, number, "read_back_mismatch", key)
            detail = "created" if inserted is not None else "already existed"
            return ToolResult(
                completed_at=self._clock(),
                action=ActionId.CREATE_CASE,
                status=ToolStatus.SUCCESS,
                verified=True,
                attempts=number,
                idempotency_key=key,
                record_id=stored.case_id,
                detail=f"Case {stored.case_id} {detail}",
            )

        return self._write(ActionId.CREATE_CASE, attempt, key)

    def _own_transaction(self, customer_id: str, transaction_id: str) -> TransactionRecord | None:
        with self._sessions() as db:
            return CoreBankingRepository(db).get_transaction(customer_id, transaction_id)

    def _new_case_id(self, db: Session) -> str:
        number = db.scalar(select(CASE_NUMBER_SEQ.next_value()))
        return f"DSP-{self._clock():%Y%m%d}-{number:06d}"

    def _read_back_case(self, customer_id: str, key: str) -> CaseRecord | None:
        """A fresh read of the case written under ``key``."""
        with self._sessions() as db:
            case_id = db.scalar(
                select(Case.case_id).where(
                    Case.customer_id == customer_id, Case.idempotency_key == key
                )
            )
            if case_id is None:
                return None
            cases = CoreBankingRepository(db).list_cases(customer_id)
            return next(case for case in cases if case.case_id == case_id)

    # ------------------------------------------------------------------ ACT-03

    def block_card(self, product_id: str) -> ToolResult:
        """ACT-03, verified by reading back ``product_status = 'Blocked'``. Repeating it on a
        blocked card succeeds without writing again."""
        customer_id = self._customer_id
        if customer_id is None:
            return self._denied(ActionId.BLOCK_CARD, "product", product_id)

        def attempt(number: int) -> ToolResult:
            product = self._read_back_product(customer_id, product_id)
            if product is None:
                return self._denied(ActionId.BLOCK_CARD, "product", product_id, None, number)
            if product.product_type not in CARD_PRODUCT_TYPES:
                return self._failed(ActionId.BLOCK_CARD, number, "product is not a card")
            if product.product_status not in BLOCKABLE_STATUSES:
                return self._failed(
                    ActionId.BLOCK_CARD, number, f"card is {product.product_status}"
                )
            if product.product_status == "Active":
                with self._sessions() as db:
                    db.execute(
                        insert(CardBlock)
                        .values(product_id=product_id, customer_id=customer_id)
                        .on_conflict_do_nothing(index_elements=[CardBlock.product_id])
                    )
                    db.commit()
            stored = self._read_back_product(customer_id, product_id)
            if stored is None or stored.product_status != "Blocked":
                return self._failed(ActionId.BLOCK_CARD, number, "read_back_mismatch")
            return ToolResult(
                completed_at=self._clock(),
                action=ActionId.BLOCK_CARD,
                status=ToolStatus.SUCCESS,
                verified=True,
                attempts=number,
                record_id=product_id,
                detail=f"Card ending {stored.product_number_masked[-4:]} blocked",
            )

        return self._write(ActionId.BLOCK_CARD, attempt)

    def _read_back_product(self, customer_id: str, product_id: str) -> ProductRecord | None:
        with self._sessions() as db:
            return CoreBankingRepository(db).get_product(customer_id, product_id)

    # ------------------------------------------------------------------ ACT-05

    def transfer_to_human(self, packet: HandoffPacket) -> ToolResult:
        """ACT-05. The queue acknowledgement is the packet written to ``handoff_packets`` and
        read back unchanged; only then is its status ``acknowledged``. Always allowed, also
        before authentication. Idempotent on ``handoff_id``."""
        customer_id = self._customer_id
        transaction_id = packet.draft_case.transaction_ref if packet.draft_case else None
        payload = packet.model_dump(mode="json")

        def attempt(number: int) -> ToolResult:
            if transaction_id is not None and (
                customer_id is None or self._own_transaction(customer_id, transaction_id) is None
            ):
                return self._denied(
                    ActionId.TRANSFER_TO_HUMAN, "transaction", transaction_id, None, number
                )
            with self._sessions() as db:
                db.execute(
                    insert(HandoffPacketRow)
                    .values(
                        handoff_id=packet.handoff_id,
                        customer_id=customer_id,
                        transaction_id=transaction_id,
                        queue=packet.queue.value,
                        priority=packet.priority.value,
                        status="pending",
                        policy_version=packet.policy_version,
                        packet=payload,
                    )
                    .on_conflict_do_nothing(index_elements=[HandoffPacketRow.handoff_id])
                )
                db.commit()
            if not self._acknowledge(packet.handoff_id, customer_id, payload):
                return self._failed(ActionId.TRANSFER_TO_HUMAN, number, "read_back_mismatch")
            return ToolResult(
                completed_at=self._clock(),
                action=ActionId.TRANSFER_TO_HUMAN,
                status=ToolStatus.SUCCESS,
                verified=True,
                attempts=number,
                record_id=packet.handoff_id,
                detail=f"Handoff {packet.handoff_id} acknowledged by the {packet.queue} queue",
            )

        return self._write(ActionId.TRANSFER_TO_HUMAN, attempt)

    def _acknowledge(
        self, handoff_id: str, customer_id: str | None, payload: dict[str, object]
    ) -> bool:
        with self._sessions() as db:
            row = db.get(HandoffPacketRow, handoff_id)
            if row is None or row.customer_id != customer_id or row.packet != payload:
                return False
            row.status = "acknowledged"
            db.commit()
            return True

    # ------------------------------------------------------------------ helpers

    def _retrying(self) -> Retrying:
        return Retrying(
            stop=stop_after_attempt(self._p.TOOL_MAX_RETRIES + 1),
            wait=wait_fixed(self._config.retry_wait_seconds),
            retry=retry_if_exception_type(TRANSIENT_ERRORS),
            reraise=True,
        )

    def _write(
        self, action: ActionId, attempt: Callable[[int], ToolResult], key: str | None = None
    ) -> ToolResult:
        """Run a write with bounded retries; a transient error after the last retry is a
        ``failed`` result, never an exception."""
        number = 0
        try:
            for retry in self._retrying():
                with retry:
                    number = retry.retry_state.attempt_number
                    return attempt(number)
        except TRANSIENT_ERRORS as exc:
            return self._failed(
                action, number, f"{type(exc).__name__} after {number} attempts", key
            )
        raise AssertionError("unreachable: tenacity either returns or raises")  # pragma: no cover

    def _failed(
        self, action: ActionId, attempts: int, error: str, key: str | None = None
    ) -> ToolResult:
        return ToolResult(
            completed_at=self._clock(),
            action=action,
            status=ToolStatus.FAILED,
            verified=False,
            attempts=max(attempts, 1),
            idempotency_key=key,
            error=error,
        )

    def _denied(
        self,
        action: ActionId,
        record_type: str,
        record_id: str,
        key: str | None = None,
        attempts: int = 1,
    ) -> ToolResult:
        reason = "unauthenticated" if self._customer_id is None else None
        self._security_event(record_type, record_id, action=action, reason=reason)
        return ToolResult(
            completed_at=self._clock(),
            action=action,
            status=ToolStatus.ACCESS_DENIED,
            verified=False,
            attempts=attempts,
            idempotency_key=key,
        )

    def _security_event(
        self,
        record_type: str,
        record_id: str | None,
        *,
        action: ActionId | None = None,
        reason: str | None = None,
    ) -> None:
        """GATE-04: the attempt is logged. The event never says whether the record exists."""
        with self._sessions() as db:
            db.add(
                AuditLog(
                    event_type="security_event",
                    conversation_id=self._conversation_id,
                    session_id=self._session.session_id if self._session else None,
                    payload={
                        "kind": EVENT_KIND,
                        "customer_id": self._customer_id,
                        "record_type": record_type,
                        "requested_id": record_id,
                        "action": action.value if action else None,
                        "reason": reason,
                    },
                )
            )
            db.commit()


def _case_matches(
    stored: CaseRecord, txn: TransactionRecord, reason_code: ReasonCode, tier: Tier
) -> bool:
    """ACT-02 read-back: transaction, reason code, tier and amount (and the ACT-04 flag)."""
    return (
        stored.transaction_id == txn.transaction_id
        and stored.reason_code is reason_code
        and stored.tier is tier
        and stored.amount == txn.amount
        and stored.currency == txn.currency
        and stored.amount_usd == txn.amount_usd
        and stored.provisional_credit_flag is CREDIT_FLAGS[tier]
    )
