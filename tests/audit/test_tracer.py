"""M11 with the database: one trace per turn, retention, listing, metrics, and the audit API."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import Connection, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit.masking import AuditMessageMode
from app.audit.tracer import DatabaseAuditTracer, DuplicateTraceError, TraceFilter
from app.config import load_policy_config
from app.contracts import Language, Outcome, TraceRecord
from app.main import create_app
from app.settings import Settings
from app.storage.models import AuditLog, SessionRow
from tests.audit.helpers import START, trace
from tests.conftest import Pipeline
from tests.fixtures.core_banking import CUSTOMER

AGENT_TOKEN = "agent-token-for-tests-0123456789abcdef"
FORBIDDEN_KEYS = {
    "customer_id",
    "document_number",
    "document_hash",
    "date_of_birth",
    "age_band",
    "gender",
    "segment",
    "credit_score",
    "estimated_monthly_income",
    "occupation",
    "email",
    "phone",
    "address",
    "full_name",
    "api_key",
    "jwt_secret",
    "password",
    "token",
}


@pytest.fixture
def session_factory(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def tracer(session_factory: sessionmaker[Session]) -> DatabaseAuditTracer:
    return DatabaseAuditTracer(session_factory)


@pytest.fixture
def session_id(db_session: Session) -> str:
    db_session.add(
        SessionRow(
            session_id="SES-AUDIT",
            customer_id=CUSTOMER,
            auth_method="test_otp",
            created_at=START,
            last_activity_at=START,
        )
    )
    db_session.flush()
    return "SES-AUDIT"


def rows(db: Session) -> int:
    return (
        db.scalar(
            select(func.count()).select_from(AuditLog).where(AuditLog.event_type == "turn_trace")
        )
        or 0
    )


# --- Recording ----------------------------------------------------------------------------------


def test_one_turn_one_trace_with_every_field(
    tracer: DatabaseAuditTracer, db_session: Session, session_id: str
) -> None:
    tracer.record(trace(session_id=session_id))
    assert rows(db_session) == 1
    payload = db_session.scalar(select(AuditLog.payload).where(AuditLog.trace_id == "TRC-1"))
    assert payload is not None
    assert set(payload) == set(TraceRecord.model_fields)
    stored = tracer.get("TRC-1")
    assert stored is not None
    assert stored.decisions[0].gates_evaluated  # gates and rules evaluated
    assert stored.model_calls[0].prompt_version == "extract@1.6.0"
    assert stored.tool_calls[0].verified is True
    assert stored.signals is not None and stored.signals.source == "llm_fallback"


def test_absent_optional_stages_are_stored_as_null(
    tracer: DatabaseAuditTracer, db_session: Session
) -> None:
    tracer.record(
        trace(
            input_guard=None,
            signals=None,
            message=None,
            handoff_id=None,
            outcome=None,
            total_latency_ms=None,
            estimated_cost_usd=None,
            language=None,
        )
    )
    payload = db_session.scalar(select(AuditLog.payload).where(AuditLog.trace_id == "TRC-1"))
    assert payload is not None
    for key in (
        "input_guard",
        "signals",
        "message",
        "handoff_id",
        "outcome",
        "language",
        "total_latency_ms",
        "estimated_cost_usd",
        "error",
        "session_id",
    ):
        assert key in payload and payload[key] is None, key


def test_a_second_trace_for_the_same_turn_is_refused(
    tracer: DatabaseAuditTracer, db_session: Session
) -> None:
    tracer.record(trace())
    with pytest.raises(DuplicateTraceError):
        tracer.record(trace())
    assert rows(db_session) == 1


def test_unknown_trace(tracer: DatabaseAuditTracer) -> None:
    assert tracer.get("TRC-NOPE") is None


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (AuditMessageMode.MASKED, "Sí, confirmo. Mi documento es [documento] y mi correo [email]"),
        (
            AuditMessageMode.FULL,
            "Sí, confirmo. Mi documento es X1234567 y mi correo ana@example.test",
        ),
        (AuditMessageMode.OMITTED, None),
    ],
)
def test_message_retention_is_applied_before_writing(
    session_factory: sessionmaker[Session], mode: AuditMessageMode, expected: str | None
) -> None:
    tracer = DatabaseAuditTracer(session_factory, mode)
    tracer.record(trace())
    stored = tracer.get("TRC-1")
    assert stored is not None and stored.message == expected


def test_trace_has_no_secrets_or_prohibited_fields(
    tracer: DatabaseAuditTracer, db_session: Session
) -> None:
    tracer.record(trace())
    payload = db_session.scalar(select(AuditLog.payload).where(AuditLog.trace_id == "TRC-1"))
    text = json.dumps(payload)

    def keys(value: object) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {k for v in value.values() for k in keys(v)}
        if isinstance(value, list):
            return {k for item in value for k in keys(item)}
        return set()

    assert keys(payload) & FORBIDDEN_KEYS == set()
    for secret in ("ana@example.test", "X1234567", AGENT_TOKEN, CUSTOMER):
        assert secret not in text


# --- Listing and metrics ------------------------------------------------------------------------


def seed(tracer: DatabaseAuditTracer) -> None:
    tracer.record(
        trace(
            "A1",
            conversation_id="CONV-A",
            turn_index=0,
            minutes=0,
            outcome=Outcome.CLARIFY,
            tool_calls=[],
        )
    )
    tracer.record(trace("A2", conversation_id="CONV-A", turn_index=1, minutes=1))
    tracer.record(
        trace(
            "B1",
            conversation_id="CONV-B",
            minutes=2,
            language=Language.PT,
            outcome=Outcome.ESCALATE,
            tool_calls=[],
            estimated_cost_usd=Decimal("0.010"),
        )
    )


def test_list_filters_and_order(tracer: DatabaseAuditTracer) -> None:
    seed(tracer)
    assert [t.trace_id for t in tracer.list(TraceFilter())] == ["B1", "A2", "A1"]
    assert [t.trace_id for t in tracer.list(TraceFilter(conversation_id="CONV-A"))] == ["A2", "A1"]
    assert [t.trace_id for t in tracer.list(TraceFilter(outcome=Outcome.ESCALATE))] == ["B1"]
    assert [t.trace_id for t in tracer.list(TraceFilter(language=Language.PT))] == ["B1"]
    window = TraceFilter(since=START + timedelta(minutes=1), until=START + timedelta(minutes=2))
    assert [t.trace_id for t in tracer.list(window)] == ["A2"]
    assert len(tracer.list(TraceFilter(limit=1))) == 1


def test_search_matches_part_of_any_id(tracer: DatabaseAuditTracer, session_id: str) -> None:
    seed(tracer)
    tracer.record(
        trace("TRC-X9", conversation_id="CONV-Z", minutes=3, handoff_id="HO-20261003-000077")
    )
    tracer.record(trace("S1", conversation_id="CONV-S", minutes=4, session_id=session_id))

    def found(text: str) -> list[str]:
        return [t.trace_id for t in tracer.list(TraceFilter(search=text))]

    assert found("conv-a") == ["A2", "A1"]  # conversation ID, any case
    assert found("b1") == ["B1"]  # trace ID
    assert found("000077") == ["TRC-X9"]  # handoff ID
    assert found(session_id[-6:]) == ["S1"]  # session ID
    assert found("  X9 ") == ["TRC-X9"]  # surrounding spaces ignored
    assert found("%") == [] and found("_") == []  # wildcards are literal
    assert found("") == ["S1", "TRC-X9", "B1", "A2", "A1"]


def test_pages_and_counts(tracer: DatabaseAuditTracer) -> None:
    seed(tracer)
    assert [t.trace_id for t in tracer.list(TraceFilter(offset=1, limit=1))] == ["A2"]
    assert tracer.count(TraceFilter(limit=1)) == 3
    assert tracer.count(TraceFilter(search="CONV-A")) == 2
    page = tracer.page(TraceFilter(offset=2, limit=2))
    assert (page.total, page.offset, page.limit) == (3, 2, 2)
    assert [(s.trace_id, s.outcome, s.total_latency_ms) for s in page.items] == [
        ("A1", Outcome.CLARIFY, 4800.0)
    ]
    assert tracer.page(TraceFilter(search="nothing")).model_dump()["items"] == []


def test_agent_searches_traces_page_by_page(
    client: TestClient, tracer: DatabaseAuditTracer
) -> None:
    seed(tracer)
    first = client.get("/api/agent/traces", params={"limit": 2}, headers=AGENT).json()
    assert first["total"] == 3 and [t["trace_id"] for t in first["items"]] == ["B1", "A2"]
    assert set(first["items"][0]) == {
        "trace_id",
        "conversation_id",
        "session_id",
        "turn_index",
        "created_at",
        "language",
        "outcome",
        "reply_kind",
        "handoff_id",
        "total_latency_ms",
        "estimated_cost_usd",
    }  # a summary: no message, decisions or model calls
    second = client.get("/api/agent/traces", params={"limit": 2, "offset": 2}, headers=AGENT)
    assert [t["trace_id"] for t in second.json()["items"]] == ["A1"]
    searched = client.get(
        "/api/agent/traces", params={"search": "conv-a", "outcome": "CLARIFY"}, headers=AGENT
    ).json()
    assert searched["total"] == 1 and searched["items"][0]["trace_id"] == "A1"
    assert (
        client.get("/api/agent/traces", params={"search": ""}, headers=AGENT).json()["total"] == 3
    )
    assert client.get("/api/agent/traces", params={"limit": 101}, headers=AGENT).status_code == 422
    assert client.get("/api/agent/traces").status_code == 403


def test_list_by_session(tracer: DatabaseAuditTracer, session_id: str) -> None:
    tracer.record(trace("S1", session_id=session_id))
    tracer.record(trace("S2"))
    assert [t.trace_id for t in tracer.list(TraceFilter(session_id=session_id))] == ["S1"]


def test_metrics_from_the_store(tracer: DatabaseAuditTracer) -> None:
    seed(tracer)
    metrics = tracer.metrics(TraceFilter())
    assert metrics.conversations == 2
    assert metrics.outcomes == {"RESOLVE": 1, "ESCALATE": 1}
    assert metrics.automated_resolutions == 1
    assert metrics.total_cost_usd == Decimal("0.018")
    assert metrics.outcomes_by_language == {"es": {"RESOLVE": 1}, "pt": {"ESCALATE": 1}}


# --- API ---------------------------------------------------------------------------------------


def settings(**values: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "business_date": datetime(2026, 6, 17).date(),
        "pseudonym_key": SecretStr("test-pseudonym-key"),
        "agent_api_token": SecretStr(AGENT_TOKEN),
    }
    base.update(values)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def client(tracer: DatabaseAuditTracer) -> Iterator[TestClient]:
    app = create_app(load_policy_config(), settings=settings(), audit_tracer=tracer)
    with TestClient(app) as test_client:
        yield test_client


AGENT = {"Authorization": f"Bearer {AGENT_TOKEN}"}


def test_agent_reads_a_trace(client: TestClient, tracer: DatabaseAuditTracer) -> None:
    tracer.record(trace())
    response = client.get("/api/audit/TRC-1", headers=AGENT)
    assert response.status_code == 200
    body = response.json()
    assert body["trace_id"] == "TRC-1"
    assert body["handoff_id"] is None  # nulls are returned, not omitted
    assert client.get("/api/audit/TRC-NOPE", headers=AGENT).status_code == 404


def test_agent_lists_and_reads_metrics(client: TestClient, tracer: DatabaseAuditTracer) -> None:
    seed(tracer)
    listed = client.get("/api/audit", params={"conversation_id": "CONV-A"}, headers=AGENT)
    assert [t["trace_id"] for t in listed.json()] == ["A2", "A1"]
    filtered = client.get(
        "/api/audit", params={"outcome": "ESCALATE", "language": "pt"}, headers=AGENT
    )
    assert [t["trace_id"] for t in filtered.json()] == ["B1"]
    metrics = client.get("/api/audit/metrics", headers=AGENT).json()
    assert metrics["outcomes"] == {"RESOLVE": 1, "ESCALATE": 1}
    assert metrics["turn_latency"]["p50_ms"] == 4800.0


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
        {"Authorization": "Basic YWdlbnQ6cGFzcw=="},
    ],
)
def test_without_the_agent_role_is_403(
    client: TestClient, tracer: DatabaseAuditTracer, headers: dict[str, str]
) -> None:
    tracer.record(trace())
    for path in ("/api/audit/TRC-1", "/api/audit", "/api/audit/metrics"):
        assert client.get(path, headers=headers).status_code == 403


def test_a_customer_session_never_reads_traces(
    session_factory: sessionmaker[Session], tracer: DatabaseAuditTracer, session_id: str
) -> None:
    from app.identity.service import IdentityService
    from tests.identity.conftest import generous_limits, identity_config

    identity = IdentityService(identity_config(), session_factory)
    issued = identity.issue_session(CUSTOMER)
    tracer.record(trace(session_id=session_id))  # even a trace of their own session
    app = create_app(
        load_policy_config(),
        identity=identity,
        rate_limits=generous_limits(),
        settings=settings(),
        audit_tracer=tracer,
    )
    with TestClient(app) as client:
        customer = {"Authorization": f"Bearer {issued.access_token}"}
        assert client.get("/auth/session", headers=customer).status_code == 200
        assert client.get("/api/audit/TRC-1", headers=customer).status_code == 403
        assert client.get("/api/audit", headers=customer).status_code == 403


def test_audit_unavailable_without_a_token_or_a_tracer(tracer: DatabaseAuditTracer) -> None:
    no_token = create_app(
        load_policy_config(), settings=settings(agent_api_token=None), audit_tracer=tracer
    )
    with TestClient(no_token) as client:
        assert client.get("/api/audit", headers=AGENT).status_code == 503
    no_tracer = create_app(load_policy_config(), settings=settings())
    with TestClient(no_tracer) as client:
        assert client.get("/api/audit", headers=AGENT).status_code == 503


def test_short_agent_token_stops_startup(tracer: DatabaseAuditTracer) -> None:
    app = create_app(
        load_policy_config(),
        settings=settings(agent_api_token=SecretStr("short")),
        audit_tracer=tracer,
    )
    with pytest.raises(ValueError, match="AGENT_API_TOKEN"), TestClient(app):
        pass


def test_tracer_built_from_settings(database_url: object) -> None:
    from sqlalchemy.engine import URL

    assert isinstance(database_url, URL)
    app = create_app(
        load_policy_config(),
        settings=settings(database_url=database_url.render_as_string(hide_password=False)),
    )
    with TestClient(app):
        assert isinstance(app.state.audit_tracer, DatabaseAuditTracer)


def test_empty_rates_mean_unknown_cost() -> None:
    assert (
        settings(llm_input_usd_per_mtok="", llm_output_usd_per_mtok=" ").llm_input_usd_per_mtok
        is None
    )
    assert settings(llm_input_usd_per_mtok="2.5").llm_input_usd_per_mtok == Decimal("2.5")


def test_created_at_of_the_row_is_the_trace_time(
    tracer: DatabaseAuditTracer, db_session: Session
) -> None:
    tracer.record(trace(minutes=5))
    created = db_session.scalar(select(AuditLog.created_at).where(AuditLog.trace_id == "TRC-1"))
    assert created == START + timedelta(minutes=5)
    assert created.tzinfo is not None and created.astimezone(UTC) == datetime(
        2026, 10, 1, 12, 5, tzinfo=UTC
    )


def test_other_integrity_errors_are_not_duplicates(tracer: DatabaseAuditTracer) -> None:
    from sqlalchemy.exc import IntegrityError

    with pytest.raises(IntegrityError):
        tracer.record(trace(session_id="SES-DOES-NOT-EXIST"))


def access_events(db: Session) -> list[dict[str, object]]:
    query = (
        select(AuditLog.payload).where(AuditLog.event_type == "audit_access").order_by(AuditLog.id)
    )
    return list(db.scalars(query).all())


def test_every_audit_read_is_recorded(
    client: TestClient, tracer: DatabaseAuditTracer, db_session: Session
) -> None:
    tracer.record(trace())
    client.get("/api/audit/TRC-1", headers=AGENT)
    client.get("/api/audit", params={"outcome": "RESOLVE", "limit": 5}, headers=AGENT)
    client.get("/api/audit/metrics", params={"language": "es"}, headers=AGENT)
    client.get("/api/audit/TRC-1")  # refused: recorded too
    events = access_events(db_session)
    assert events == [
        {"endpoint": "/api/audit/TRC-1", "granted": True, "query": {}, "trace_id": "TRC-1"},
        {"endpoint": "/api/audit", "granted": True, "query": {"outcome": "RESOLVE", "limit": "5"}},
        {"endpoint": "/api/audit/metrics", "granted": True, "query": {"language": "es"}},
        {"endpoint": "/api/audit/TRC-1", "granted": False, "query": {}, "trace_id": "TRC-1"},
    ]
    times = db_session.scalars(
        select(AuditLog.created_at).where(AuditLog.event_type == "audit_access")
    ).all()
    assert all(t is not None for t in times)
    # The token itself is never recorded.
    assert AGENT_TOKEN not in json.dumps(events)
