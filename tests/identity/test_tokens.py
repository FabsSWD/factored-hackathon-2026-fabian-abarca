from __future__ import annotations

import base64
import json

import jwt
import pytest

from app.identity import tokens
from tests.identity.conftest import SECRET

CLAIMS = tokens.TokenClaims(customer_id="CLI-A", session_id="SES-A", iat=1_000, exp=4_600)


def _b64(data: dict[str, object]) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()


def test_round_trip() -> None:
    assert tokens.decode(tokens.encode(CLAIMS, SECRET), SECRET) == CLAIMS


def test_header_is_hs256() -> None:
    assert jwt.get_unverified_header(tokens.encode(CLAIMS, SECRET))["alg"] == "HS256"


def test_expired_claims_still_decode() -> None:
    # Expiry is the service's job, with its injected clock.
    old = tokens.TokenClaims(customer_id="CLI-A", session_id="SES-A", iat=1, exp=2)
    assert tokens.decode(tokens.encode(old, SECRET), SECRET).exp == 2


def test_alg_none_is_rejected() -> None:
    unsigned = f"{_b64({'alg': 'none', 'typ': 'JWT'})}.{_b64(CLAIMS.model_dump())}."
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(unsigned, SECRET)


@pytest.mark.parametrize("algorithm", ["HS384", "HS512"])
def test_other_algorithms_are_rejected(algorithm: str) -> None:
    # Same secret, padded only so PyJWT accepts it for the longer digest; the alg is refused.
    token = jwt.encode(CLAIMS.model_dump(), SECRET * 2, algorithm=algorithm)
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(token, SECRET * 2)


def test_other_key_is_rejected() -> None:
    token = tokens.encode(CLAIMS, "another-secret-that-is-also-long-enough")
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(token, SECRET)


def test_tampered_payload_is_rejected() -> None:
    header, _, signature = tokens.encode(CLAIMS, SECRET).split(".")
    forged = CLAIMS.model_dump() | {"customer_id": "CLI-B"}
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(f"{header}.{_b64(forged)}.{signature}", SECRET)


@pytest.mark.parametrize("claim", ["customer_id", "session_id", "iat", "exp"])
def test_missing_claim_is_rejected(claim: str) -> None:
    payload = {k: v for k, v in CLAIMS.model_dump().items() if k != claim}
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(jwt.encode(payload, SECRET, algorithm="HS256"), SECRET)


def test_unexpected_claim_is_rejected() -> None:
    payload = CLAIMS.model_dump() | {"role": "agent"}
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(jwt.encode(payload, SECRET, algorithm="HS256"), SECRET)


@pytest.mark.parametrize("token", ["", "garbage", "a.b.c", "a.b"])
def test_malformed_tokens_are_rejected(token: str) -> None:
    with pytest.raises(tokens.InvalidTokenError):
        tokens.decode(token, SECRET)


def test_short_secret_is_refused() -> None:
    with pytest.raises(ValueError, match="at least 32 bytes"):
        tokens.check_secret("short")
    tokens.check_secret("x" * 32)
