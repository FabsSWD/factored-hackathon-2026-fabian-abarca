"""FastAPI dependencies shared by routers: identity, authenticated session, rate limits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.contracts import SessionContext
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityService
from app.settings import Settings

INVALID_SESSION = "invalid_session"

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class RateLimits:
    per_session: RateLimiter
    auth_per_ip: RateLimiter

    @classmethod
    def from_settings(cls, settings: Settings) -> RateLimits:
        return cls(
            per_session=RateLimiter(settings.rate_limit_requests_per_minute),
            auth_per_ip=RateLimiter(settings.auth_rate_limit_per_minute),
        )


def get_identity(request: Request) -> IdentityService:
    identity: IdentityService | None = request.app.state.identity
    if identity is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "identity_unavailable")
    return identity


def _too_many(retry_after: int) -> HTTPException:
    return HTTPException(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
        headers={"Retry-After": str(retry_after)},
    )


def limit_by_ip(request: Request) -> None:
    """For /auth/login and /auth/verify, which have no session yet."""
    limits: RateLimits = request.app.state.rate_limits
    client = request.client.host if request.client else "unknown"
    retry_after = limits.auth_per_ip.hit(f"ip:{client}")
    if retry_after is not None:
        raise _too_many(retry_after)


def require_session(
    request: Request,
    identity: Annotated[IdentityService, Depends(get_identity)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> SessionContext:
    """The authenticated session. The customer ID comes only from the token (GATE-04)."""
    unauthorized = HTTPException(
        status.HTTP_401_UNAUTHORIZED, INVALID_SESSION, headers={"WWW-Authenticate": "Bearer"}
    )
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise unauthorized
    session = identity.validate_session(credentials.credentials)
    if session is None:
        raise unauthorized
    limits: RateLimits = request.app.state.rate_limits
    retry_after = limits.per_session.hit(f"session:{session.session_id}")
    if retry_after is not None:
        raise _too_many(retry_after)
    return session


CurrentSession = Annotated[SessionContext, Depends(require_session)]
Identity = Annotated[IdentityService, Depends(get_identity)]
