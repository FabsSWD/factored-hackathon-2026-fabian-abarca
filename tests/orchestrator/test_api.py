"""POST /api/turn through the real application, with the Orchestrator's doubles."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.api.dependencies import RateLimits
from app.config import load_policy_config
from app.contracts import Confirmation, ReasonCode
from app.identity.rate_limit import RateLimiter
from app.main import create_app
from app.settings import Settings
from tests.orchestrator.fakes import OTHER_TOKEN, REF_CAFE, TOKEN, World, build_world, ext

AMOUNT = "Me cobraron 50 en Cafe Sintetico el 10 de junio y acordamos 40"


def settings() -> Settings:
    return Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        pseudonym_key=SecretStr("test-pseudonym-key"),
    )


def limits(per_minute: int = 1000) -> RateLimits:
    return RateLimits(per_session=RateLimiter(per_minute), auth_per_ip=RateLimiter(1000))


@pytest.fixture
def world() -> World:
    return build_world()


@pytest.fixture
def client(world: World) -> Iterator[TestClient]:
    app = create_app(
        load_policy_config(),
        settings=settings(),
        orchestrator=world.orchestrator,
        rate_limits=limits(),
    )
    with TestClient(app) as test_client:
        yield test_client


def post(
    client: TestClient, message: str, conversation_id: str | None = None, token: str | None = TOKEN
) -> dict[str, object]:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    body: dict[str, object] = {"message": message}
    if conversation_id:
        body["conversation_id"] = conversation_id
    response = client.post("/api/turn", json=body, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()  # type: ignore[no-any-return]


def test_a_conversation_through_the_api(client: TestClient, world: World) -> None:
    world.say(
        AMOUNT,
        ext(
            transaction_ref=REF_CAFE,
            reason_code=ReasonCode.INCORRECT_AMOUNT,
            expected_amount=Decimal("40"),
        ),
    )
    first = post(client, AMOUNT)
    cid = str(first["conversation_id"])
    assert first["turn_index"] == 0 and first["language"] == "es"
    assert "sí, confirmo" in str(first["reply"])
    world.say("sí, confirmo", ext(confirmation=Confirmation.CONFIRMED))
    second = post(client, "sí, confirmo", cid)
    assert second["turn_index"] == 1
    assert "Registramos su disputa con la referencia DSP-" in str(second["reply"])
    assert second["handed_off"] is False
    assert set(second) == {
        "conversation_id",
        "turn_index",
        "reply",
        "language",
        "handed_off",
        "trace_id",
        "status",  # M14: which state the chat shows; never a rule, threshold or score
        "case_reference",
        "handoff_reference",  # the tracking number of a handoff, only in the turn that hands off
    }
    assert first["status"] == "awaiting_confirmation" and first["case_reference"] is None
    assert second["status"] == "case_created"
    assert str(second["case_reference"]).startswith("DSP-")
    assert str(second["case_reference"]) in str(second["reply"])  # read back, then shown
    assert first["handoff_reference"] is None and second["handoff_reference"] is None


def test_without_a_session_the_turn_runs_without_account_data(
    client: TestClient, world: World
) -> None:
    result = post(client, "Hola, quiero disputar un cargo de mi tarjeta", token=None)
    assert "verificar su identidad" in str(result["reply"])
    assert result["status"] == "authentication_required"
    assert world.bank.reads == []


def test_another_customer_gets_403(client: TestClient) -> None:
    first = post(client, "Hola, quiero disputar un cargo de mi tarjeta")
    response = client.post(
        "/api/turn",
        json={"message": "hola", "conversation_id": first["conversation_id"]},
        headers={"Authorization": f"Bearer {OTHER_TOKEN}"},
    )
    assert response.status_code == 403
    assert response.json() == {"detail": "conversation_not_available"}


@pytest.mark.parametrize(
    "body",
    [
        {"message": ""},
        {"message": "x" * 2001},
        {"message": "hola", "conversation_id": "a b"},
        {"message": "hola", "extra": 1},
    ],
)
def test_invalid_requests_are_rejected(client: TestClient, body: dict[str, object]) -> None:
    assert client.post("/api/turn", json=body).status_code == 422


def test_rate_limit(world: World) -> None:
    app = create_app(
        load_policy_config(),
        settings=settings(),
        orchestrator=world.orchestrator,
        rate_limits=limits(per_minute=1),
    )
    with TestClient(app) as client:
        assert client.post("/api/turn", json={"message": "hola"}).status_code == 200
        limited = client.post("/api/turn", json={"message": "hola"})
        assert limited.status_code == 429 and "Retry-After" in limited.headers


def test_chat_unavailable_without_an_orchestrator() -> None:
    with TestClient(create_app(load_policy_config(), settings=settings())) as client:
        assert client.post("/api/turn", json={"message": "hola"}).status_code == 503
        assert client.get("/api/agent/handoffs").status_code == 503


def test_unexpected_errors_are_not_http_errors(world: World) -> None:
    world.bank.crash_on = "products"
    app = create_app(
        load_policy_config(),
        settings=settings(),
        orchestrator=world.orchestrator,
        rate_limits=limits(),
    )
    with TestClient(app) as client:
        result = post(client, "Hola, quiero disputar un cargo de mi tarjeta")
    assert result["handed_off"] is True
    assert "No pude confirmar" in str(result["reply"])
    assert "Traceback" not in str(result["reply"]) and "tool bug" not in str(result["reply"])
