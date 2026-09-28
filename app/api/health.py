"""Liveness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from pydantic import BaseModel

router = APIRouter()


class HealthResponse(BaseModel):
    status: str
    policy_version: str


@router.get("/health")
def health(request: Request) -> HealthResponse:
    return HealthResponse(
        status="ok", policy_version=request.app.state.policy_config.policy_version
    )
