"""Identity Service (architecture §3): test-OTP authentication and sessions (GATE-02).

A mock of a real identity provider. The customer proves identity with a document number and
a one-time code; knowing a customer ID or a document alone never creates a session.

- Login: the document is looked up by its HMAC (DATA-06). A challenge is stored for every
  request, whether or not the document exists, so the response never reveals existence.
- Verify: unknown document, wrong or expired OTP, and a locked document all raise the same
  error. OTP_MAX_FAILURES failures within OTP_FAILURE_WINDOW_MIN lock the document; a correct
  OTP is refused while locked. Every failure is a security event in the audit log, carrying
  the document's HMAC, never the document or the OTP.
- Sessions: JWT (HS256) with customer_id, session_id, iat and exp, backed by a session row.
  A session is valid while younger than SESSION_MAX_AGE_MIN, idle less than
  SESSION_IDLE_TIMEOUT_MIN, and not revoked. Time comes from an injected real-time clock.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.config import PolicyParameters
from app.contracts import SessionContext
from app.identity import tokens
from app.identity.clock import Clock, utc_now
from app.settings import Settings
from app.storage.data_contract import document_hash
from app.storage.models import AuditLog, Customer, OtpChallenge, OtpFailure, SessionRow

AUTH_METHOD_OTP = "test_otp"
AUTH_METHOD_INTERNAL = "internal"


class InvalidCredentialsError(Exception):
    """One error for unknown document, wrong or expired OTP and locked document."""


class IdentityNotConfiguredError(RuntimeError):
    """A setting the Identity Service needs is missing."""


@dataclass(frozen=True)
class IdentityConfig:
    jwt_secret: str
    test_otp: str
    document_hash_key: str
    session_max_age: timedelta
    session_idle_timeout: timedelta
    otp_ttl: timedelta
    otp_max_failures: int
    otp_failure_window: timedelta

    def __post_init__(self) -> None:
        tokens.check_secret(self.jwt_secret)
        if not self.test_otp:
            raise IdentityNotConfiguredError("TEST_OTP is not set")
        if not self.document_hash_key:
            raise IdentityNotConfiguredError("DOCUMENT_HASH_KEY is not set")

    @classmethod
    def from_settings(cls, settings: Settings, policy: PolicyParameters) -> IdentityConfig:
        missing = [
            name
            for name, value in (
                ("JWT_SECRET", settings.jwt_secret),
                ("TEST_OTP", settings.test_otp),
                ("DOCUMENT_HASH_KEY", settings.document_hash_key),
            )
            if value is None or not value.get_secret_value()
        ]
        if missing:
            raise IdentityNotConfiguredError(f"missing settings: {', '.join(missing)}")
        assert settings.jwt_secret and settings.test_otp and settings.document_hash_key
        return cls(
            jwt_secret=settings.jwt_secret.get_secret_value(),
            test_otp=settings.test_otp.get_secret_value(),
            document_hash_key=settings.document_hash_key.get_secret_value(),
            session_max_age=timedelta(minutes=policy.SESSION_MAX_AGE_MIN),
            session_idle_timeout=timedelta(minutes=policy.SESSION_IDLE_TIMEOUT_MIN),
            otp_ttl=timedelta(minutes=settings.otp_ttl_min),
            otp_max_failures=settings.otp_max_failures,
            otp_failure_window=timedelta(minutes=settings.otp_failure_window_min),
        )


class IssuedSession(BaseModel):
    model_config = ConfigDict(frozen=True)

    access_token: str
    token_type: str = "bearer"
    session_id: str
    expires_at: AwareDatetime


class IdentityService:
    def __init__(
        self,
        config: IdentityConfig,
        session_factory: sessionmaker[Session],
        clock: Clock = utc_now,
    ) -> None:
        self._config = config
        self._sessions = session_factory
        self._clock = clock

    @property
    def otp_ttl(self) -> timedelta:
        return self._config.otp_ttl

    # --- OTP ------------------------------------------------------------------

    def request_otp(self, document_number: str) -> None:
        """Start (or restart) an OTP challenge. Behaves the same for unknown documents."""
        now = self._now()
        key = self._hash(document_number)
        with self._sessions() as db:
            challenge = db.get(OtpChallenge, key)
            if challenge is None:
                challenge = OtpChallenge(document_hash=key)
                db.add(challenge)
            challenge.issued_at = now
            challenge.expires_at = now + self._config.otp_ttl
            db.commit()

    def verify_otp(self, document_number: str, otp: str) -> IssuedSession:
        now = self._now()
        key = self._hash(document_number)
        with self._sessions() as db:
            failures = db.scalar(
                select(func.count())
                .select_from(OtpFailure)
                .where(
                    OtpFailure.document_hash == key,
                    OtpFailure.failed_at > now - self._config.otp_failure_window,
                )
            )
            if (failures or 0) >= self._config.otp_max_failures:
                self._security_event(db, "otp_locked", document_hash=key)
                db.commit()
                raise InvalidCredentialsError

            challenge = db.get(OtpChallenge, key)
            customer_id = db.scalar(
                select(Customer.customer_id).where(Customer.document_hash == key)
            )
            otp_matches = hmac.compare_digest(otp.encode(), self._config.test_otp.encode())
            challenge_valid = challenge is not None and challenge.expires_at > now

            if not (challenge_valid and customer_id is not None and otp_matches):
                db.add(OtpFailure(document_hash=key, failed_at=now))
                self._security_event(
                    db,
                    "otp_failed",
                    document_hash=key,
                    failures_in_window=(failures or 0) + 1,
                    reason=_failure_reason(challenge_valid, customer_id is not None),
                )
                db.commit()
                raise InvalidCredentialsError

            assert customer_id is not None
            db.execute(delete(OtpChallenge).where(OtpChallenge.document_hash == key))
            db.execute(delete(OtpFailure).where(OtpFailure.document_hash == key))
            issued = self._create_session(db, customer_id, AUTH_METHOD_OTP, now)
            db.commit()
            return issued

    # --- Sessions -------------------------------------------------------------

    def issue_session(self, customer_id: str) -> IssuedSession:
        """Internal only (tests and the evaluation harness); never exposed over HTTP."""
        now = self._now()
        with self._sessions() as db:
            if db.get(Customer, customer_id) is None:
                raise ValueError("unknown customer")
            issued = self._create_session(db, customer_id, AUTH_METHOD_INTERNAL, now)
            db.commit()
            return issued

    def validate_session(self, token: str) -> SessionContext | None:
        """The session for a valid token, refreshing its activity; ``None`` otherwise."""
        try:
            claims = tokens.decode(token, self._config.jwt_secret)
        except tokens.InvalidTokenError:
            with self._sessions() as db:
                self._security_event(db, "invalid_token")
                db.commit()
            return None

        now = self._now()
        if tokens.timestamp(now) >= claims.exp:
            return None
        with self._sessions() as db:
            row = db.get(SessionRow, claims.session_id, with_for_update=True)
            if (
                row is None
                or row.customer_id != claims.customer_id
                or row.revoked_at is not None
                or now - row.created_at >= self._config.session_max_age
                or now - row.last_activity_at >= self._config.session_idle_timeout
            ):
                return None
            row.last_activity_at = max(now, row.last_activity_at)
            context = SessionContext(
                session_id=row.session_id,
                customer_id=row.customer_id,
                auth_method=row.auth_method,
                issued_at=row.created_at,
                last_activity_at=row.last_activity_at,
            )
            db.commit()
            return context

    def expires_at(self, context: SessionContext) -> datetime:
        return context.issued_at + self._config.session_max_age

    def revoke(self, session_id: str) -> None:
        now = self._now()
        with self._sessions() as db:
            row = db.get(SessionRow, session_id, with_for_update=True)
            if row is not None and row.revoked_at is None:
                row.revoked_at = max(now, row.created_at)
                db.commit()

    # --- Internals ------------------------------------------------------------

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None:
            raise ValueError("the identity clock must return timezone-aware datetimes")
        return now

    def _hash(self, document_number: str) -> str:
        return document_hash(document_number, self._config.document_hash_key)

    def _create_session(
        self, db: Session, customer_id: str, method: str, now: datetime
    ) -> IssuedSession:
        session_id = secrets.token_urlsafe(32)
        db.add(
            SessionRow(
                session_id=session_id,
                customer_id=customer_id,
                auth_method=method,
                created_at=now,
                last_activity_at=now,
            )
        )
        expires = now + self._config.session_max_age
        claims = tokens.TokenClaims(
            customer_id=customer_id,
            session_id=session_id,
            iat=tokens.timestamp(now),
            exp=tokens.timestamp(expires),
        )
        return IssuedSession(
            access_token=tokens.encode(claims, self._config.jwt_secret),
            session_id=session_id,
            expires_at=expires,
        )

    @staticmethod
    def _security_event(db: Session, kind: str, **details: Any) -> None:
        # Only the document's HMAC may appear here; never the document or the OTP.
        db.add(AuditLog(event_type="security_event", payload={"kind": kind, **details}))


def _failure_reason(challenge_valid: bool, known_document: bool) -> str:
    if not known_document:
        return "unknown_document"
    if not challenge_valid:
        return "no_active_challenge"
    return "wrong_otp"
