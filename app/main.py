"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.health import router as health_router
from app.config import PolicyConfig, load_policy_config


def create_app(policy_config: PolicyConfig | None = None) -> FastAPI:
    """Build the application. The policy is loaded and validated before serving requests."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.policy_config = policy_config or load_policy_config()
        yield

    app = FastAPI(title="Dispute Intake API", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    return app


app = create_app()
