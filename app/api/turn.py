"""Customer chat API (M12): one customer message per turn.

``POST /api/turn`` takes the message and, optionally, the conversation it continues and the
session token from ``/auth/verify``. Without a valid session the turn still runs, but no account
data is read (GATE-02). The response carries the reply text and no rule identifier, threshold,
or score (COM-07, ACT-06).

``status`` tells the customer chat (M14) which state to show, from what the reply does:

- ``handed_off``: transferred to an agent; the chat disables its input;
- ``case_created``: ACT-02 created and read back the case, whose reference is
  ``case_reference`` (COM-04: never before the read-back);
- ``authentication_required``: the reply asks the customer to log in (no session, or it
  expired); the chat opens its login and keeps the conversation;
- ``awaiting_confirmation``: the reply asks for a yes or a no (the case summary or the card
  block offer, COM-03);
- ``in_progress``: anything else.
"""

from __future__ import annotations

import hashlib
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field

from app.api.dependencies import RateLimits
from app.contracts import Language
from app.orchestrator.service import ConversationAccessError, Orchestrator, TurnResult

router = APIRouter(prefix="/api", tags=["chat"])
_bearer = HTTPBearer(auto_error=False)
MAX_MESSAGE_CHARS = 2000
TurnStatus = Literal[
    "in_progress", "awaiting_confirmation", "authentication_required", "case_created", "handed_off"
]
CONFIRMATION_KINDS = frozenset({"summary", "clarify:confirmation", "block_offer"})


class TurnRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{4,64}$")
    message: str = Field(min_length=1, max_length=MAX_MESSAGE_CHARS)


class TurnResponse(BaseModel):
    conversation_id: str
    turn_index: int
    reply: str
    language: Language | None
    handed_off: bool  # the conversation was transferred to a human agent
    trace_id: str
    status: TurnStatus
    case_reference: str | None = None  # only with status case_created


def turn_status(result: TurnResult) -> TurnStatus:
    if result.closed:
        return "handed_off"
    if result.reply_kind == "case_created" and result.case_reference:
        return "case_created"
    if result.reply_kind == "clarify:authentication":
        return "authentication_required"
    if result.reply_kind in CONFIRMATION_KINDS:
        return "awaiting_confirmation"
    return "in_progress"


def get_orchestrator(request: Request) -> Orchestrator:
    orchestrator: Orchestrator | None = getattr(request.app.state, "orchestrator", None)
    if orchestrator is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "chat_unavailable")
    return orchestrator


def _limit(request: Request, token: str | None) -> None:
    limits: RateLimits = request.app.state.rate_limits
    if token:
        key = "turn:" + hashlib.sha256(token.encode()).hexdigest()[:24]
    else:
        key = f"turn-ip:{request.client.host if request.client else 'unknown'}"
    retry_after = limits.per_session.hit(key)
    if retry_after is not None:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            "rate_limited",
            headers={"Retry-After": str(retry_after)},
        )


@router.post("/turn")
async def turn(
    body: TurnRequest,
    request: Request,
    orchestrator: Annotated[Orchestrator, Depends(get_orchestrator)],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
) -> TurnResponse:
    token = credentials.credentials if credentials is not None else None
    _limit(request, token)
    try:
        result = await orchestrator.handle_turn(body.conversation_id, body.message, token)
    except ConversationAccessError:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "conversation_not_available") from None
    return TurnResponse(
        conversation_id=result.conversation_id,
        turn_index=result.turn_index,
        reply=result.reply,
        language=result.language,
        handed_off=result.closed,
        trace_id=result.trace_id,
        status=turn_status(result),
        case_reference=result.case_reference if result.reply_kind == "case_created" else None,
    )
