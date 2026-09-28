"""Authentication endpoints (Identity Service, GATE-02)."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from app.api.dependencies import CurrentSession, Identity, limit_by_ip
from app.identity.service import InvalidCredentialsError, IssuedSession

router = APIRouter(prefix="/auth", tags=["auth"])

INVALID_CREDENTIALS = "invalid_credentials"


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_number: str = Field(min_length=1, max_length=32)


class LoginResponse(BaseModel):
    status: str = "otp_sent"
    expires_in_seconds: int


class VerifyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    document_number: str = Field(min_length=1, max_length=32)
    # Any string: a malformed OTP must fail exactly like a wrong one.
    otp: str = Field(min_length=1, max_length=16)


class SessionResponse(BaseModel):
    session_id: str
    customer_id: str
    issued_at: AwareDatetime
    last_activity_at: AwareDatetime
    expires_at: datetime


@router.post(
    "/login",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(limit_by_ip)],
)
def login(body: LoginRequest, identity: Identity) -> LoginResponse:
    """Request an OTP. The response is identical whether or not the document exists."""
    identity.request_otp(body.document_number)
    return LoginResponse(expires_in_seconds=int(identity.otp_ttl.total_seconds()))


@router.post("/verify", dependencies=[Depends(limit_by_ip)])
def verify(body: VerifyRequest, identity: Identity) -> IssuedSession:
    try:
        return identity.verify_otp(body.document_number, body.otp)
    except InvalidCredentialsError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, INVALID_CREDENTIALS) from None


@router.get("/session")
def current_session(session: CurrentSession, identity: Identity) -> SessionResponse:
    return SessionResponse(
        session_id=session.session_id,
        customer_id=session.customer_id,
        issued_at=session.issued_at,
        last_activity_at=session.last_activity_at,
        expires_at=identity.expires_at(session),
    )


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(session: CurrentSession, identity: Identity) -> Response:
    identity.revoke(session.session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
