from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection
from sqlalchemy.orm import Session, sessionmaker

from app.api.dependencies import RateLimits
from app.config import load_policy_config
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityConfig, IdentityService
from app.main import create_app
from tests.conftest import TEST_HASH_KEY, Pipeline

SECRET = "test-jwt-secret-that-is-long-enough-000"
OTP = "482913"
DOCUMENT = "X1234567"  # tests.fixtures.core_banking.CUSTOMER
OTHER_DOCUMENT = "42388496"  # tests.fixtures.core_banking.OTHER_CUSTOMER
UNKNOWN_DOCUMENT = "Z0000000"
START = datetime(2026, 9, 28, 12, 0, tzinfo=UTC)


@dataclass
class FakeClock:
    """Injected real-time clock; tests move it instead of sleeping."""

    now: datetime = field(default=START)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> None:
        self.now += timedelta(**delta)


def identity_config(**overrides: object) -> IdentityConfig:
    values: dict[str, object] = {
        "jwt_secret": SECRET,
        "test_otp": OTP,
        "document_hash_key": TEST_HASH_KEY,
        "session_max_age": timedelta(minutes=60),
        "session_idle_timeout": timedelta(minutes=15),
        "otp_ttl": timedelta(minutes=5),
        "otp_max_failures": 3,
        "otp_failure_window": timedelta(minutes=15),
    }
    values.update(overrides)
    return IdentityConfig(**values)  # type: ignore[arg-type]


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def session_factory(loaded: Pipeline, connection: Connection) -> sessionmaker[Session]:
    return sessionmaker(
        bind=connection, join_transaction_mode="create_savepoint", expire_on_commit=False
    )


@pytest.fixture
def identity(session_factory: sessionmaker[Session], clock: FakeClock) -> IdentityService:
    return IdentityService(identity_config(), session_factory, clock)


def generous_limits() -> RateLimits:
    return RateLimits(per_session=RateLimiter(1000), auth_per_ip=RateLimiter(1000))


@pytest.fixture
def client(identity: IdentityService) -> Iterator[TestClient]:
    app = create_app(load_policy_config(), identity=identity, rate_limits=generous_limits())
    with TestClient(app) as test_client:
        yield test_client
