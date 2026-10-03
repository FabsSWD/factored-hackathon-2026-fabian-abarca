"""The system under evaluation, served for real in this process (M18).

``build_app`` builds the application exactly as ``create_app`` does from settings, except that
the Orchestrator's model clients and Tool Layer go through the harness wrappers (faults per
conversation, signal capture) and the abuse limits are raised for the simulated customers.
``HarnessServer`` serves it with uvicorn on a free local port, so the simulator talks HTTP to
the real routes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from types import TracebackType

import uvicorn
from fastapi import FastAPI
from sqlalchemy import Engine

from app.api.dependencies import RateLimits
from app.audit.tracer import DatabaseAuditTracer
from app.config import PolicyConfig
from app.contracts import HandoffPacket, TraceRecord
from app.evaluation.harness.faults import (
    Capture,
    FaultRegistry,
    HarnessDecision,
    HarnessLLM,
    HarnessOrchestrator,
    harness_tools,
)
from app.handoff.queue import DatabaseHandoffQueue
from app.identity.rate_limit import RateLimiter
from app.identity.service import IdentityConfig, IdentityService
from app.main import create_app
from app.orchestrator.factory import orchestrator_parts
from app.settings import Settings
from app.storage.database import make_engine, make_session_factory

SIMULATED_LIMIT = 100_000  # requests per minute: the simulator is not an abuser


@dataclass
class HarnessApp:
    app: FastAPI
    tracer: DatabaseAuditTracer
    queue: DatabaseHandoffQueue
    engine: Engine

    def trace(self, trace_id: str) -> TraceRecord | None:
        return self.tracer.get(trace_id)

    def handoff(self, handoff_id: str) -> HandoffPacket | None:
        return self.queue.get(handoff_id)


def build_app(
    settings: Settings, policy: PolicyConfig, faults: FaultRegistry, capture: Capture
) -> HarnessApp:
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is not set")
    engine = make_engine(settings.database_url)
    sessions = make_session_factory(engine)
    tracer = DatabaseAuditTracer(sessions, settings.audit_message_mode)
    identity = IdentityService(IdentityConfig.from_settings(settings, policy.parameters), sessions)
    parts = orchestrator_parts(settings, policy, sessions, identity, tracer)
    parts["llm"] = HarnessLLM(parts["llm"], faults, capture)
    parts["decision"] = HarnessDecision(parts["decision"], faults, capture)
    parts["tools"] = harness_tools(parts["tools"], faults, policy.parameters.TOOL_MAX_RETRIES + 1)
    queue = DatabaseHandoffQueue(sessions)
    app = create_app(
        policy,
        identity=identity,
        rate_limits=RateLimits(RateLimiter(SIMULATED_LIMIT), RateLimiter(SIMULATED_LIMIT)),
        settings=settings,
        audit_tracer=tracer,
        orchestrator=HarnessOrchestrator(**parts),
        handoff_queue=queue,
    )
    return HarnessApp(app, tracer, queue, engine)


class HarnessServer:
    """uvicorn in a background thread, on 127.0.0.1 and a free port."""

    def __init__(self, app: FastAPI, startup_seconds: float = 30.0) -> None:
        config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", lifespan="on")
        self._server = uvicorn.Server(config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self._startup = startup_seconds
        self.base_url = ""

    def __enter__(self) -> HarnessServer:
        self._thread.start()
        deadline = time.monotonic() + self._startup
        while not self._server.started:
            if time.monotonic() > deadline or not self._thread.is_alive():
                raise RuntimeError("the harness server did not start")
            time.sleep(0.05)
        port = self._server.servers[0].sockets[0].getsockname()[1]
        self.base_url = f"http://127.0.0.1:{port}"
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        trace: TracebackType | None,
    ) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=30)
