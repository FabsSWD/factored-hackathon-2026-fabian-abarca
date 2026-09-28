"""Session tokens: JWT signed with HS256 only.

The algorithm is fixed. A token whose header names any other algorithm, including ``none``,
is rejected before its signature is checked. Expiry is checked by the Identity Service with
its injected clock, not by PyJWT's wall clock, so time-dependent tests are exact.
"""

from __future__ import annotations

from datetime import datetime

import jwt
from pydantic import BaseModel, ConfigDict, ValidationError

ALGORITHM = "HS256"
MIN_SECRET_BYTES = 32
REQUIRED_CLAIMS = ("customer_id", "session_id", "iat", "exp")


class InvalidTokenError(Exception):
    """The token is malformed, tampered with, or not signed with the expected key and alg."""


class TokenClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    customer_id: str
    session_id: str
    iat: int
    exp: int


def check_secret(secret: str) -> None:
    if len(secret.encode()) < MIN_SECRET_BYTES:
        raise ValueError(f"JWT_SECRET must be at least {MIN_SECRET_BYTES} bytes long")


def encode(claims: TokenClaims, secret: str) -> str:
    return jwt.encode(claims.model_dump(), secret, algorithm=ALGORITHM)


def decode(token: str, secret: str) -> TokenClaims:
    try:
        header = jwt.get_unverified_header(token)
        if header.get("alg") != ALGORITHM:
            raise InvalidTokenError("unexpected algorithm")
        payload = jwt.decode(
            token,
            secret,
            algorithms=[ALGORITHM],
            options={
                "require": list(REQUIRED_CLAIMS),
                "verify_exp": False,
                "verify_iat": False,
            },
        )
        return TokenClaims.model_validate(payload)
    except (jwt.PyJWTError, ValidationError) as exc:
        raise InvalidTokenError(str(exc)) from exc


def timestamp(moment: datetime) -> int:
    return int(moment.timestamp())
