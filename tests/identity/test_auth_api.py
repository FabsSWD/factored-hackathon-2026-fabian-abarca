from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import create_engine, delete
from sqlalchemy.engine import URL

from app.api.dependencies import RateLimits
from app.config import load_policy_config
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityService
from app.main import create_app
from app.settings import Settings
from app.storage.models import OtpChallenge
from tests.conftest import TEST_HASH_KEY
from tests.fixtures.core_banking import CUSTOMER, OTHER_CUSTOMER
from tests.identity.conftest import (
    DOCUMENT,
    OTHER_DOCUMENT,
    OTP,
    SECRET,
    UNKNOWN_DOCUMENT,
    FakeClock,
    generous_limits,
)

WIRING_DOCUMENT = "WIRING00"
INVALID = {"detail": "invalid_credentials"}


def login(client: TestClient, document: str = DOCUMENT, otp: str = OTP) -> str:
    assert client.post("/auth/login", json={"document_number": document}).status_code == 202
    response = client.post("/auth/verify", json={"document_number": document, "otp": otp})
    assert response.status_code == 200, response.text
    token = response.json()["access_token"]
    assert isinstance(token, str)
    return token


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --- Login and verify ---------------------------------------------------------------


def test_login_response_is_identical_for_known_and_unknown_documents(client: TestClient) -> None:
    known = client.post("/auth/login", json={"document_number": DOCUMENT})
    unknown = client.post("/auth/login", json={"document_number": UNKNOWN_DOCUMENT})
    assert known.status_code == unknown.status_code == 202
    assert known.json() == unknown.json() == {"status": "otp_sent", "expires_in_seconds": 300}


def test_verify_returns_a_bearer_token(client: TestClient) -> None:
    client.post("/auth/login", json={"document_number": DOCUMENT})
    body = client.post("/auth/verify", json={"document_number": DOCUMENT, "otp": OTP}).json()
    assert body["token_type"] == "bearer"
    assert set(body) == {"access_token", "token_type", "session_id", "expires_at"}


def test_same_error_for_unknown_document_wrong_otp_and_locked(client: TestClient) -> None:
    client.post("/auth/login", json={"document_number": UNKNOWN_DOCUMENT})
    unknown = client.post("/auth/verify", json={"document_number": UNKNOWN_DOCUMENT, "otp": OTP})

    client.post("/auth/login", json={"document_number": DOCUMENT})
    wrong = client.post("/auth/verify", json={"document_number": DOCUMENT, "otp": "000000"})
    malformed = client.post("/auth/verify", json={"document_number": DOCUMENT, "otp": "abc"})
    client.post("/auth/verify", json={"document_number": DOCUMENT, "otp": "111111"})
    locked = client.post("/auth/verify", json={"document_number": DOCUMENT, "otp": OTP})

    for response in (unknown, wrong, malformed, locked):
        assert response.status_code == 401
        assert response.json() == INVALID
        assert set(response.headers) == set(unknown.headers)


def test_customer_id_in_the_body_is_rejected(client: TestClient) -> None:
    login_body = {"document_number": DOCUMENT, "customer_id": OTHER_CUSTOMER}
    assert client.post("/auth/login", json=login_body).status_code == 422
    verify_body = {"document_number": DOCUMENT, "otp": OTP, "customer_id": OTHER_CUSTOMER}
    assert client.post("/auth/verify", json=verify_body).status_code == 422


def test_missing_fields_are_rejected(client: TestClient) -> None:
    assert client.post("/auth/login", json={}).status_code == 422
    assert client.post("/auth/verify", json={"document_number": DOCUMENT}).status_code == 422


# --- Authenticated routes -------------------------------------------------------------


def test_session_endpoint_uses_the_customer_from_the_token(client: TestClient) -> None:
    token = login(client)
    response = client.get(f"/auth/session?customer_id={OTHER_CUSTOMER}", headers=bearer(token))
    assert response.status_code == 200
    assert response.json()["customer_id"] == CUSTOMER
    other = login(client, OTHER_DOCUMENT)
    assert (
        client.get("/auth/session", headers=bearer(other)).json()["customer_id"] == OTHER_CUSTOMER
    )


def test_missing_token_is_401_with_challenge(client: TestClient) -> None:
    response = client.get("/auth/session")
    assert response.status_code == 401
    assert response.json() == {"detail": "invalid_session"}
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_non_bearer_scheme_is_401(client: TestClient) -> None:
    token = login(client)
    assert (
        client.get("/auth/session", headers={"Authorization": f"Basic {token}"}).status_code == 401
    )


def test_tampered_token_is_401(client: TestClient) -> None:
    token = login(client)
    assert client.get("/auth/session", headers=bearer(token + "x")).status_code == 401


def test_expired_session_is_401(client: TestClient, clock: FakeClock) -> None:
    token = login(client)
    clock.advance(minutes=15)
    assert client.get("/auth/session", headers=bearer(token)).status_code == 401


def test_logout_revokes_the_session(client: TestClient) -> None:
    token = login(client)
    response = client.post(
        "/auth/logout", headers=bearer(token), json={"customer_id": OTHER_CUSTOMER}
    )
    assert response.status_code == 204
    assert client.get("/auth/session", headers=bearer(token)).status_code == 401
    assert client.post("/auth/logout", headers=bearer(token)).status_code == 401


def test_logout_only_affects_its_own_session(client: TestClient) -> None:
    first = login(client)
    second = login(client)
    client.post("/auth/logout", headers=bearer(first))
    assert client.get("/auth/session", headers=bearer(second)).status_code == 200


# --- Rate limits ---------------------------------------------------------------------


def test_login_and_verify_are_limited_per_ip(identity: IdentityService) -> None:
    limits = RateLimits(per_session=RateLimiter(1000), auth_per_ip=RateLimiter(3))
    with TestClient(create_app(load_policy_config(), identity=identity, rate_limits=limits)) as c:
        c.post("/auth/login", json={"document_number": DOCUMENT})
        c.post("/auth/verify", json={"document_number": DOCUMENT, "otp": "000000"})
        c.post("/auth/login", json={"document_number": DOCUMENT})
        limited = c.post("/auth/login", json={"document_number": DOCUMENT})
    assert limited.status_code == 429
    assert limited.json() == {"detail": "rate_limited"}
    assert int(limited.headers["Retry-After"]) >= 1


def test_authenticated_routes_are_limited_per_session(identity: IdentityService) -> None:
    limits = RateLimits(per_session=RateLimiter(2), auth_per_ip=RateLimiter(1000))
    with TestClient(create_app(load_policy_config(), identity=identity, rate_limits=limits)) as c:
        first, second = login(c), login(c)
        assert c.get("/auth/session", headers=bearer(first)).status_code == 200
        assert c.get("/auth/session", headers=bearer(first)).status_code == 200
        assert c.get("/auth/session", headers=bearer(first)).status_code == 429
        assert c.get("/auth/session", headers=bearer(second)).status_code == 200


def test_invalid_tokens_do_not_consume_a_session_quota(identity: IdentityService) -> None:
    limits = RateLimits(per_session=RateLimiter(1), auth_per_ip=RateLimiter(1000))
    with TestClient(create_app(load_policy_config(), identity=identity, rate_limits=limits)) as c:
        token = login(c)
        for _ in range(3):
            assert c.get("/auth/session", headers=bearer("bad")).status_code == 401
        assert c.get("/auth/session", headers=bearer(token)).status_code == 200


# --- Wiring --------------------------------------------------------------------------


def test_auth_is_unavailable_without_identity_configuration() -> None:
    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        pseudonym_key=SecretStr("test-pseudonym-key"),
    )
    with TestClient(create_app(load_policy_config(), settings=settings)) as c:
        response = c.post("/auth/login", json={"document_number": DOCUMENT})
        assert response.status_code == 503
        assert c.get("/health").status_code == 200


def test_identity_is_built_from_complete_settings(database_url: URL) -> None:
    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        pseudonym_key=SecretStr("test-pseudonym-key"),
        database_url=database_url.render_as_string(hide_password=False),
        jwt_secret=SecretStr(SECRET),
        test_otp=SecretStr(OTP),
        document_hash_key=SecretStr(TEST_HASH_KEY),
    )
    engine = create_engine(database_url)
    try:
        with TestClient(create_app(load_policy_config(), settings=settings)) as c:
            assert c.app.state.identity is not None  # type: ignore[attr-defined]
            # This app commits for real: use a document no other test uses, then clean up.
            assert (
                c.post("/auth/login", json={"document_number": WIRING_DOCUMENT}).status_code == 202
            )
    finally:
        with engine.begin() as conn:
            conn.execute(delete(OtpChallenge))
        engine.dispose()


def test_incomplete_settings_leave_identity_unavailable(database_url: URL) -> None:
    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        pseudonym_key=SecretStr("test-pseudonym-key"),
        database_url=database_url.render_as_string(hide_password=False),
        test_otp=SecretStr(OTP),
    )
    with TestClient(create_app(load_policy_config(), settings=settings)) as c:
        assert c.post("/auth/login", json={"document_number": WIRING_DOCUMENT}).status_code == 503


def test_generous_limits_helper() -> None:
    assert generous_limits().per_session.hit("k") is None
