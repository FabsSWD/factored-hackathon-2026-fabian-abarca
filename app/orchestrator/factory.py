"""Builds the Orchestrator and its modules from settings (M12)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from app.audit.cost import rates_from_settings
from app.audit.tracer import DatabaseAuditTracer
from app.config import PolicyConfig
from app.contracts import SessionContext
from app.decision.factory import decision_client_from_settings
from app.handoff import PolicyHandoffBuilder, SequenceHandoffIds
from app.identity.service import IdentityService
from app.input_guard.service import RuleBasedInputGuard
from app.interfaces import ToolLayer
from app.llm_adapter.factory import adapter_from_settings
from app.orchestrator.calls import record_call
from app.orchestrator.service import Orchestrator, OrchestratorConfig
from app.orchestrator.state import InMemoryConversationStore
from app.policy import DeterministicPolicyEngine
from app.settings import Settings
from app.templates.service import TemplateService
from app.tools import DatabaseToolLayer, ToolConfig


def orchestrator_from_settings(
    settings: Settings,
    policy: PolicyConfig,
    session_factory: sessionmaker[Session],
    identity: IdentityService | None,
    tracer: DatabaseAuditTracer,
) -> Orchestrator:
    return Orchestrator(**orchestrator_parts(settings, policy, session_factory, identity, tracer))


def orchestrator_parts(
    settings: Settings,
    policy: PolicyConfig,
    session_factory: sessionmaker[Session],
    identity: IdentityService | None,
    tracer: DatabaseAuditTracer,
) -> dict[str, Any]:
    """The Orchestrator's modules built from settings, as its keyword arguments. The M18 harness
    wraps some of them (fault injection, signal capture) before building it."""
    parameters = policy.parameters
    pseudonym_key = settings.require_pseudonym_key()
    tool_config = ToolConfig(as_of=settings.as_of, parameters=parameters)

    def tools(session: SessionContext | None, conversation_id: str) -> ToolLayer:
        return DatabaseToolLayer(
            session_factory, session, tool_config, conversation_id=conversation_id
        )

    return dict(
        identity=identity,
        guard=RuleBasedInputGuard(session_factory, parameters.INJECTION_STRIKES_MAX),
        llm=adapter_from_settings(settings, recorder=record_call),
        decision=decision_client_from_settings(settings, recorder=record_call),
        engine=DeterministicPolicyEngine(policy),
        tools=tools,
        builder=PolicyHandoffBuilder(
            parameters, pseudonym_key, SequenceHandoffIds(session_factory)
        ),
        templates=TemplateService.from_policy(parameters),
        tracer=tracer,
        store=InMemoryConversationStore(),
        config=OrchestratorConfig(
            as_of=settings.as_of,
            parameters=parameters,
            policy_version=policy.policy_version,
            pseudonym_key=pseudonym_key,
            turn_deadline_seconds=settings.llm_turn_deadline_seconds,
            token_cap=settings.llm_max_tokens_per_conversation,
            rates=rates_from_settings(settings),
        ),
    )
