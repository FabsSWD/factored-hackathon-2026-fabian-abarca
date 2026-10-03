"""Agent console API (M12): the queue of escalated cases and each packet, from the database,
and the Orchestrator built from settings."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel, SecretStr
from sqlalchemy import select
from sqlalchemy.engine import URL
from sqlalchemy.orm import Session, sessionmaker

from app.audit.tracer import DatabaseAuditTracer, TracePage, TraceSummary
from app.config import load_policy_config
from app.contracts import HandoffPacket, Priority, Queue, TraceRecord
from app.handoff.examples import example_packets
from app.handoff.queue import DatabaseHandoffQueue, HandoffFilter, HandoffPage, HandoffSummary
from app.main import create_app
from app.orchestrator.service import Orchestrator
from app.settings import Settings
from app.storage.models import AuditLog
from tests.tools.conftest import ToolFactory

AGENT_TOKEN = "agent-token-for-tests-0123456789abcdef"
AGENT = {"Authorization": f"Bearer {AGENT_TOKEN}"}


def settings(**values: object) -> Settings:
    base: dict[str, object] = {
        "_env_file": None,
        "business_date": date(2026, 6, 17),
        "pseudonym_key": SecretStr("test-pseudonym-key"),
        "agent_api_token": SecretStr(AGENT_TOKEN),
    }
    base.update(values)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture
def queue(session_factory: sessionmaker[Session], make_tools: ToolFactory) -> DatabaseHandoffQueue:
    tools = make_tools(None)  # transfers before authentication are allowed (ACT-05)
    for packet in example_packets().values():
        if packet.draft_case is None:  # the examples' records are not in this database
            assert tools.transfer_to_human(packet).verified
    return DatabaseHandoffQueue(session_factory)


@pytest.fixture
def client(
    queue: DatabaseHandoffQueue, session_factory: sessionmaker[Session]
) -> Iterator[TestClient]:
    app = create_app(
        load_policy_config(),
        settings=settings(),
        audit_tracer=DatabaseAuditTracer(session_factory),
        handoff_queue=queue,
    )
    with TestClient(app) as test_client:
        yield test_client


def test_queue_orders_high_priority_then_oldest(queue: DatabaseHandoffQueue) -> None:
    summaries = queue.list(HandoffFilter())
    assert [s.queue for s in summaries] == [Queue.SECURITY_REVIEW, Queue.DISPUTES]
    assert all(s.status == "acknowledged" for s in summaries)
    assert queue.list(HandoffFilter(queue=Queue.DISPUTES))[0].request_summary.startswith(
        "Customer not authenticated"
    )
    assert queue.list(HandoffFilter(priority=Priority.HIGH)) == []


def test_agent_reads_the_queue_and_a_packet(client: TestClient) -> None:
    listed = client.get("/api/agent/handoffs", headers=AGENT)
    assert listed.status_code == 200
    page = listed.json()
    assert (page["total"], page["offset"], page["limit"]) == (2, 0, 20)
    rows = page["items"]
    assert {r["queue"] for r in rows} == {"security_review", "disputes"}
    detail = client.get(f"/api/agent/handoffs/{rows[0]['handoff_id']}", headers=AGENT)
    assert detail.status_code == 200
    assert detail.json()["handoff_id"] == rows[0]["handoff_id"]
    assert client.get("/api/agent/handoffs/HO-20990101-000001", headers=AGENT).status_code == 404
    filtered = client.get("/api/agent/handoffs", params={"queue": "disputes"}, headers=AGENT)
    assert [r["queue"] for r in filtered.json()["items"]] == ["disputes"]


def test_queue_is_paged(client: TestClient, queue: DatabaseHandoffQueue) -> None:
    first = client.get("/api/agent/handoffs", params={"limit": 1}, headers=AGENT).json()
    second = client.get(
        "/api/agent/handoffs", params={"limit": 1, "offset": 1}, headers=AGENT
    ).json()
    assert first["total"] == second["total"] == 2
    assert [r["queue"] for r in first["items"] + second["items"]] == ["security_review", "disputes"]
    assert queue.count(HandoffFilter(queue=Queue.FRAUD)) == 0
    assert (
        client.get("/api/agent/handoffs", params={"limit": 101}, headers=AGENT).status_code == 422
    )


def test_agent_metrics(client: TestClient) -> None:
    response = client.get("/api/agent/metrics", headers=AGENT)
    assert response.status_code == 200
    assert response.json()["conversations"] == 0


def test_agent_session_check(client: TestClient) -> None:
    response = client.get("/api/agent/session", headers=AGENT)
    assert response.status_code == 204
    assert response.content == b""


# DATA-02 attributes and the identifiers DATA-06 keeps out (or only as an HMAC).
FORBIDDEN_FIELDS = {
    "customer_id",
    "document_number",
    "date_of_birth",
    "gender",
    "segment",
    "credit_score",
    "estimated_monthly_income",
    "detected_accent",
    "occupation",
    "marital_status",
    "education_level",
    "country",
    "full_name",
    "email",
    "phone",
    "address",
}


def _keys(value: object) -> set[str]:
    if isinstance(value, dict):
        return set(value) | {k for v in value.values() for k in _keys(v)}
    if isinstance(value, list):
        return {k for v in value for k in _keys(v)}
    return set()


def test_the_console_never_receives_prohibited_fields(client: TestClient) -> None:
    rows = client.get("/api/agent/handoffs", headers=AGENT).json()["items"]
    assert not _keys(rows) & FORBIDDEN_FIELDS
    for row in rows:
        packet = client.get(f"/api/agent/handoffs/{row['handoff_id']}", headers=AGENT).json()
        assert not _keys(packet) & FORBIDDEN_FIELDS
        # The customer is named by the pseudonymous reference, never by the customer ID.
        assert packet["customer_ref"].startswith("CUS-")
        for fact in packet["verified_facts"]:
            assert fact["source"] and fact["record_id"]


@pytest.mark.parametrize(
    "model", [HandoffPage, HandoffSummary, HandoffPacket, TracePage, TraceSummary, TraceRecord]
)
def test_console_responses_have_no_field_for_prohibited_data(model: type[BaseModel]) -> None:
    schema = model.model_json_schema()
    properties = set(schema.get("properties", {}))
    for definition in schema.get("$defs", {}).values():
        properties |= set(definition.get("properties", {}))
    assert not properties & FORBIDDEN_FIELDS


@pytest.mark.parametrize(
    "path", ["/api/agent/session", "/api/agent/handoffs", "/api/agent/traces", "/api/agent/metrics"]
)
def test_agent_console_needs_the_agent_role(
    client: TestClient, db_session: Session, path: str
) -> None:
    assert client.get(path).status_code == 403
    assert client.get(path, headers={"Authorization": "Bearer customer-token"}).status_code == 403
    events: list[dict[str, object]] = list(
        db_session.scalars(
            select(AuditLog.payload).where(AuditLog.event_type == "audit_access")
        ).all()
    )
    assert [e["granted"] for e in events] == [False, False]


def test_orchestrator_is_built_from_settings(database_url: URL) -> None:
    app = create_app(
        load_policy_config(),
        settings=settings(
            database_url=database_url.render_as_string(hide_password=False),
            openai_api_key=SecretStr("test-key-not-used"),
            llm_model="gpt-6-luna",
            llm_max_tokens_per_conversation="",
        ),
    )
    with TestClient(app):
        assert isinstance(app.state.orchestrator, Orchestrator)
        assert isinstance(app.state.handoff_queue, DatabaseHandoffQueue)


def test_chat_unavailable_without_an_llm_key(database_url: URL) -> None:
    app = create_app(
        load_policy_config(),
        settings=settings(database_url=database_url.render_as_string(hide_password=False)),
    )
    with TestClient(app) as client:
        assert app.state.orchestrator is None
        assert client.post("/api/turn", json={"message": "hola"}).status_code == 503
