from app.contracts import Confirmation, ReasonCode, TransactionRef
from datetime import date
from decimal import Decimal
from tests.e2e.conftest import E2E
from tests.orchestrator.fakes import ext


def test_resolve(e2e: E2E) -> None:
    chat = e2e.conversation()
    ref = TransactionRef(merchant="Cafe Sintetico", amount=Decimal("50"), transaction_date=date(2026, 6, 16))
    t = chat.send("No reconozco 50 dólares de Cafe Sintetico del 16 de junio", ext(transaction_ref=ref, reason_code=ReasonCode.UNRECOGNIZED, card_in_possession=True, shared_credentials=False))
    print(t.status, t.reply, t.outcome, t.rules)
    t = chat.send("no, no la bloquee", ext(confirmation=Confirmation.DECLINED))
    print(t.reply, t.outcome)
    t = chat.send("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    print(t.reply, t.outcome, [c.case_id for c in e2e.cases()])
