"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.agent import router as agent_router
from app.api.audit import router as audit_router
from app.api.auth import router as auth_router
from app.api.dependencies import RateLimits
from app.api.health import router as health_router
from app.api.turn import router as turn_router
from app.audit.tracer import DatabaseAuditTracer
from app.config import PolicyConfig, load_policy_config
from app.handoff.queue import DatabaseHandoffQueue
from app.identity.service import IdentityConfig, IdentityNotConfiguredError, IdentityService
from app.orchestrator.factory import orchestrator_from_settings
from app.orchestrator.service import Orchestrator
from app.settings import Settings, get_settings
from app.storage.database import make_engine, make_session_factory


def create_app(
    policy_config: PolicyConfig | None = None,
    *,
    identity: IdentityService | None = None,
    rate_limits: RateLimits | None = None,
    settings: Settings | None = None,
    audit_tracer: DatabaseAuditTracer | None = None,
    orchestrator: Orchestrator | None = None,
    handoff_queue: DatabaseHandoffQueue | None = None,
) -> FastAPI:
    """Build the application. The policy is loaded and validated before serving requests.

    Tests inject ``identity``, ``rate_limits``, ``audit_tracer``, ``orchestrator`` and
    ``handoff_queue``; otherwise they are built from settings. Without a database the chat, the
    agent console and the audit API answer 503; without the Identity Service configured, the
    authentication endpoints do. Startup fails without PSEUDONYM_KEY.
    """

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        policy = policy_config or load_policy_config()
        app.state.policy_config = policy
        resolved = settings or get_settings()
        app.state.pseudonym_key = resolved.require_pseudonym_key()
        app.state.agent_token = resolved.agent_token()
        engine = None
        tracer = audit_tracer
        if tracer is None and resolved.database_url:
            engine = make_engine(resolved.database_url)
            tracer = DatabaseAuditTracer(make_session_factory(engine), resolved.audit_message_mode)
        app.state.audit_tracer = tracer
        service = identity
        if service is None or rate_limits is None:
            app.state.rate_limits = rate_limits or RateLimits.from_settings(resolved)
            if service is None and resolved.database_url:
                try:
                    config = IdentityConfig.from_settings(resolved, policy.parameters)
                except IdentityNotConfiguredError:
                    config = None
                if config is not None:
                    engine = engine or make_engine(resolved.database_url)
                    service = IdentityService(config, make_session_factory(engine))
        else:
            app.state.rate_limits = rate_limits
        app.state.identity = service
        chat = orchestrator
        queue = handoff_queue
        if resolved.database_url and (chat is None or queue is None):
            engine = engine or make_engine(resolved.database_url)
            session_factory = make_session_factory(engine)
            queue = queue or DatabaseHandoffQueue(session_factory)
            if chat is None and tracer is not None:
                try:
                    chat = orchestrator_from_settings(
                        resolved, policy, session_factory, service, tracer
                    )
                except ValueError:
                    chat = None  # e.g. no OPENAI_API_KEY: the chat answers 503
        app.state.orchestrator = chat
        app.state.handoff_queue = queue
        try:
            yield
        finally:
            if engine is not None:
                engine.dispose()

    app = FastAPI(title="Dispute Intake API", version="0.1.0", lifespan=lifespan)
    app.include_router(health_router)
    app.include_router(auth_router)
    app.include_router(audit_router)
    app.include_router(turn_router)
    app.include_router(agent_router)
    return app


app = create_app()
