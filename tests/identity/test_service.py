from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.config import load_policy_config
from app.identity import tokens
from app.identity.service import (
    AUTH_METHOD_INTERNAL,
    AUTH_METHOD_OTP,
    IdentityConfig,
    IdentityNotConfiguredError,
    IdentityService,
    InvalidCredentialsError,
)
from app.settings import Settings
from app.storage.models import AuditLog, OtpFailure, SessionRow
from tests.conftest import TEST_HASH_KEY
from tests.fixtures.core_banking import CUSTOMER, OTHER_CUSTOMER
from tests.identity.conftest import (
    DOCUMENT,
    OTHER_DOCUMENT,
    OTP,
    SECRET,
    UNKNOWN_DOCUMENT,
    FakeClock,
    identity_config,
)


def login(identity: IdentityService, document: str = DOCUMENT, otp: str = OTP) -> str:
    identity.request_otp(document)
    return identity.verify_otp(document, otp).access_token


def security_events(db: Session) -> list[dict[str, object]]:
    rows = db.scalars(
        select(AuditLog).where(AuditLog.event_type == "security_event").order_by(AuditLog.id)
    )
    return [row.payload for row in rows]


def fail(identity: IdentityService, document: str = DOCUMENT, otp: str = "000000") -> None:
    with pytest.raises(InvalidCredentialsError):
        identity.verify_otp(document, otp)


# --- OTP login ------------------------------------------------------------------


def test_document_and_otp_create_a_session(identity: IdentityService, db_session: Session) -> None:
    identity.request_otp(DOCUMENT)
    issued = identity.verify_otp(DOCUMENT, OTP)
    assert issued.token_type == "bearer"
    context = identity.validate_session(issued.access_token)
    assert context is not None
    assert context.customer_id == CUSTOMER
    assert context.session_id == issued.session_id
    assert context.auth_method == AUTH_METHOD_OTP
    row = db_session.get(SessionRow, issued.session_id)
    assert row is not None
    assert row.revoked_at is None


def test_claims_carry_customer_session_iat_and_exp(
    identity: IdentityService, clock: FakeClock
) -> None:
    identity.request_otp(DOCUMENT)
    issued = identity.verify_otp(DOCUMENT, OTP)
    claims = jwt.decode(
        issued.access_token, SECRET, algorithms=["HS256"], options={"verify_exp": False}
    )
    assert set(claims) == {"customer_id", "session_id", "iat", "exp"}
    assert claims["customer_id"] == CUSTOMER
    assert claims["iat"] == int(clock.now.timestamp())
    assert claims["exp"] == claims["iat"] + 3600
    assert issued.expires_at == clock.now + timedelta(minutes=60)


@pytest.mark.parametrize("typed", ["x1234567", " X1234567 ", "X-123.4567"])
def test_document_input_is_normalized(identity: IdentityService, typed: str) -> None:
    identity.request_otp(typed)
    assert identity.verify_otp(typed, OTP).access_token


def test_verify_without_login_fails(identity: IdentityService) -> None:
    fail(identity, otp=OTP)


def test_unknown_document_fails_even_with_the_right_otp(identity: IdentityService) -> None:
    identity.request_otp(UNKNOWN_DOCUMENT)
    fail(identity, UNKNOWN_DOCUMENT, OTP)


def test_wrong_otp_fails(identity: IdentityService) -> None:
    identity.request_otp(DOCUMENT)
    fail(identity)


def test_otp_expires_at_ttl(identity: IdentityService, clock: FakeClock) -> None:
    identity.request_otp(DOCUMENT)
    clock.advance(minutes=5)
    fail(identity, otp=OTP)


def test_otp_is_valid_just_before_ttl(identity: IdentityService, clock: FakeClock) -> None:
    identity.request_otp(DOCUMENT)
    clock.advance(minutes=5, microseconds=-1)
    assert identity.verify_otp(DOCUMENT, OTP).access_token


def test_challenge_is_single_use(identity: IdentityService) -> None:
    login(identity)
    fail(identity, otp=OTP)


def test_a_new_login_restarts_the_challenge(identity: IdentityService, clock: FakeClock) -> None:
    identity.request_otp(DOCUMENT)
    clock.advance(minutes=4)
    identity.request_otp(DOCUMENT)
    clock.advance(minutes=4)
    assert identity.verify_otp(DOCUMENT, OTP).access_token


# --- Lockout --------------------------------------------------------------------


def test_fourth_attempt_is_locked_even_with_the_right_otp(identity: IdentityService) -> None:
    identity.request_otp(DOCUMENT)
    for _ in range(3):
        fail(identity)
    fail(identity, otp=OTP)


def test_lock_lifts_when_failures_leave_the_window(
    identity: IdentityService, clock: FakeClock
) -> None:
    identity.request_otp(DOCUMENT)
    for _ in range(3):
        fail(identity)
    clock.advance(minutes=15)
    identity.request_otp(DOCUMENT)
    assert identity.verify_otp(DOCUMENT, OTP).access_token


def test_lock_holds_until_the_window_boundary(identity: IdentityService, clock: FakeClock) -> None:
    identity.request_otp(DOCUMENT)
    for _ in range(3):
        fail(identity)
    clock.advance(minutes=15, microseconds=-1)
    identity.request_otp(DOCUMENT)
    fail(identity, otp=OTP)


def test_lock_is_per_document(identity: IdentityService) -> None:
    identity.request_otp(DOCUMENT)
    for _ in range(3):
        fail(identity)
    assert login(identity, OTHER_DOCUMENT)


def test_unknown_documents_are_locked_too(identity: IdentityService, db_session: Session) -> None:
    identity.request_otp(UNKNOWN_DOCUMENT)
    for _ in range(4):
        fail(identity, UNKNOWN_DOCUMENT)
    kinds = [event["kind"] for event in security_events(db_session)]
    assert kinds == ["otp_failed", "otp_failed", "otp_failed", "otp_locked"]


def test_success_clears_previous_failures(identity: IdentityService, db_session: Session) -> None:
    identity.request_otp(DOCUMENT)
    fail(identity)
    fail(identity)
    identity.verify_otp(DOCUMENT, OTP)
    assert db_session.scalars(select(OtpFailure)).all() == []
    identity.request_otp(DOCUMENT)
    fail(identity)
    fail(identity)
    assert identity.verify_otp(DOCUMENT, OTP).access_token


# --- Security events --------------------------------------------------------------


def test_every_failed_otp_is_a_security_event(
    identity: IdentityService, db_session: Session
) -> None:
    identity.request_otp(DOCUMENT)
    fail(identity)
    identity.request_otp(UNKNOWN_DOCUMENT)
    fail(identity, UNKNOWN_DOCUMENT, OTP)
    events = security_events(db_session)
    assert [e["kind"] for e in events] == ["otp_failed", "otp_failed"]
    assert [e["reason"] for e in events] == ["wrong_otp", "unknown_document"]
    assert events[0]["failures_in_window"] == 1


def test_no_log_or_event_contains_the_document_or_the_otp(
    identity: IdentityService, db_session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    identity.request_otp(DOCUMENT)
    for _ in range(3):
        fail(identity, otp="999111")
    fail(identity, otp=OTP)  # locked
    identity.request_otp(UNKNOWN_DOCUMENT)
    fail(identity, UNKNOWN_DOCUMENT, OTP)
    identity.validate_session("not-a-token")

    dumped = json.dumps(security_events(db_session)) + caplog.text
    for secret in (DOCUMENT, UNKNOWN_DOCUMENT, OTP, "999111"):
        assert secret not in dumped
    assert len(security_events(db_session)) == 6


def test_invalid_token_is_a_security_event(identity: IdentityService, db_session: Session) -> None:
    assert identity.validate_session("garbage") is None
    assert [e["kind"] for e in security_events(db_session)] == ["invalid_token"]


# --- Session validity (GATE-02) -----------------------------------------------------


def test_session_expires_exactly_at_max_age(identity: IdentityService, clock: FakeClock) -> None:
    token = login(identity)
    for _ in range(4):  # keep the session active
        clock.advance(minutes=14)
        assert identity.validate_session(token) is not None
    clock.advance(minutes=3, seconds=59)  # 59:59 after issue
    assert identity.validate_session(token) is not None
    clock.advance(seconds=1)  # exactly 60:00
    assert identity.validate_session(token) is None


def test_session_expires_exactly_at_idle_timeout(
    identity: IdentityService, clock: FakeClock
) -> None:
    token = login(identity)
    clock.advance(minutes=15)
    assert identity.validate_session(token) is None


def test_session_is_valid_just_before_idle_timeout(
    identity: IdentityService, clock: FakeClock
) -> None:
    token = login(identity)
    clock.advance(minutes=15, microseconds=-1)
    assert identity.validate_session(token) is not None


def test_validation_refreshes_last_activity(
    identity: IdentityService, clock: FakeClock, db_session: Session
) -> None:
    token = login(identity)
    clock.advance(minutes=10)
    context = identity.validate_session(token)
    assert context is not None
    assert context.last_activity_at == clock.now
    clock.advance(minutes=10)  # 20 min after login, 10 after last activity
    assert identity.validate_session(token) is not None


def test_revoked_session_is_invalid(identity: IdentityService, db_session: Session) -> None:
    token = login(identity)
    context = identity.validate_session(token)
    assert context is not None
    identity.revoke(context.session_id)
    identity.revoke(context.session_id)  # idempotent
    identity.revoke("SES-UNKNOWN")  # no error
    assert identity.validate_session(token) is None
    row = db_session.get(SessionRow, context.session_id)
    assert row is not None
    assert row.revoked_at is not None


def test_token_signed_with_another_key_is_invalid(identity: IdentityService) -> None:
    token = login(identity)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    forged = jwt.encode(claims, "another-secret-that-is-also-long-enough", algorithm="HS256")
    assert identity.validate_session(forged) is None


def test_alg_none_token_is_invalid(identity: IdentityService) -> None:
    token = login(identity)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    unsigned = jwt.encode(claims, None, algorithm="none")
    assert identity.validate_session(unsigned) is None


def test_tampered_token_is_invalid(identity: IdentityService) -> None:
    token = login(identity)
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload[:-2]}AA.{signature}"
    assert identity.validate_session(tampered) is None


def test_validly_signed_token_for_another_customer_is_invalid(identity: IdentityService) -> None:
    # Defense in depth: the session row must belong to the customer named in the token.
    token = login(identity)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    forged = tokens.encode(tokens.TokenClaims(**(claims | {"customer_id": OTHER_CUSTOMER})), SECRET)
    assert identity.validate_session(forged) is None


def test_validly_signed_token_for_unknown_session_is_invalid(
    identity: IdentityService, clock: FakeClock
) -> None:
    now = int(clock.now.timestamp())
    claims = tokens.TokenClaims(customer_id=CUSTOMER, session_id="SES-X", iat=now, exp=now + 60)
    assert identity.validate_session(tokens.encode(claims, SECRET)) is None


def test_token_past_its_exp_claim_is_invalid(identity: IdentityService, clock: FakeClock) -> None:
    token = login(identity)
    claims = jwt.decode(token, SECRET, algorithms=["HS256"], options={"verify_exp": False})
    short = tokens.encode(tokens.TokenClaims(**(claims | {"exp": claims["iat"] + 60})), SECRET)
    clock.advance(seconds=60)
    assert identity.validate_session(short) is None


def test_naive_clock_is_refused(session_factory: sessionmaker[Session]) -> None:
    service = IdentityService(identity_config(), session_factory, lambda: datetime(2026, 9, 28))
    with pytest.raises(ValueError, match="timezone-aware"):
        service.request_otp(DOCUMENT)


# --- Internal issuance ------------------------------------------------------------


def test_issue_session_for_tests_and_harness(identity: IdentityService) -> None:
    issued = identity.issue_session(CUSTOMER)
    context = identity.validate_session(issued.access_token)
    assert context is not None
    assert context.customer_id == CUSTOMER
    assert context.auth_method == AUTH_METHOD_INTERNAL


def test_issue_session_refuses_unknown_customer(identity: IdentityService) -> None:
    with pytest.raises(ValueError, match="unknown customer"):
        identity.issue_session("CLI-NOBODY")


# --- Configuration --------------------------------------------------------------


def test_short_jwt_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="JWT_SECRET"):
        identity_config(jwt_secret="short")


@pytest.mark.parametrize("field", ["test_otp", "document_hash_key"])
def test_empty_secrets_are_refused(field: str) -> None:
    with pytest.raises(IdentityNotConfiguredError):
        identity_config(**{field: ""})


def test_config_from_settings_uses_policy_limits() -> None:
    settings = Settings(
        _env_file=None,
        business_date=date(2026, 6, 17),
        jwt_secret=SecretStr(SECRET),
        test_otp=SecretStr(OTP),
        document_hash_key=SecretStr(TEST_HASH_KEY),
        otp_failure_window_min=30,
    )
    config = IdentityConfig.from_settings(settings, load_policy_config().parameters)
    assert config.session_max_age == timedelta(minutes=60)
    assert config.session_idle_timeout == timedelta(minutes=15)
    assert config.otp_max_failures == 3
    assert config.otp_failure_window == timedelta(minutes=30)
    assert config.otp_ttl == timedelta(minutes=5)


def test_config_from_settings_names_missing_values() -> None:
    settings = Settings(_env_file=None, business_date=date(2026, 6, 17), test_otp=SecretStr(OTP))
    with pytest.raises(IdentityNotConfiguredError, match="JWT_SECRET, DOCUMENT_HASH_KEY"):
        IdentityConfig.from_settings(settings, load_policy_config().parameters)
