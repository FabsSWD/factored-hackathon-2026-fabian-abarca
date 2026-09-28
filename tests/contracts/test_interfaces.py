from __future__ import annotations

import inspect

import pytest

from app import interfaces
from app.contracts import (
    InputGuardResult,
    Language,
    LLMContext,
    ModelSignals,
    ModelSource,
)

PROTOCOLS = [
    interfaces.IdentityService,
    interfaces.InputGuard,
    interfaces.LLMAdapter,
    interfaces.DecisionClient,
    interfaces.PolicyEngine,
    interfaces.ToolLayer,
    interfaces.HandoffBuilder,
    interfaces.TemplateService,
    interfaces.AuditTracer,
]


class FakeGuard:
    def inspect(self, session_id: str, message: str) -> InputGuardResult:
        return InputGuardResult(flagged=False, strikes=0)


class FakeDecisionClient:
    async def signals(self, message: str, context: LLMContext) -> ModelSignals:
        return ModelSignals(source=ModelSource.UNAVAILABLE)


class FakeTemplates:
    def render(self, template_id: str, language: Language, **values: object) -> str:
        return template_id


def test_fakes_satisfy_their_protocols() -> None:
    assert isinstance(FakeGuard(), interfaces.InputGuard)
    assert isinstance(FakeDecisionClient(), interfaces.DecisionClient)
    assert isinstance(FakeTemplates(), interfaces.TemplateService)


def test_incomplete_implementation_is_not_accepted() -> None:
    assert not isinstance(FakeGuard(), interfaces.PolicyEngine)


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=lambda p: p.__name__)
def test_no_interface_accepts_a_customer_id(protocol: type) -> None:
    # GATE-04: the customer always comes from the session, never from a caller or a model.
    for name, member in inspect.getmembers(protocol, inspect.isfunction):
        assert "customer_id" not in inspect.signature(member).parameters, f"{protocol}.{name}"


def test_model_calls_are_async() -> None:
    assert inspect.iscoroutinefunction(interfaces.LLMAdapter.extract)
    assert inspect.iscoroutinefunction(interfaces.LLMAdapter.connect)
    assert inspect.iscoroutinefunction(interfaces.DecisionClient.signals)


def test_policy_engine_is_synchronous() -> None:
    assert not inspect.iscoroutinefunction(interfaces.PolicyEngine.evaluate)


def test_tool_layer_exposes_no_prohibited_action() -> None:
    # ACT-06 actions must not exist, not even as guarded methods.
    forbidden = (
        "refund",
        "reverse",
        "credit",
        "close",
        "reopen",
        "unblock",
        "update_contact",
        "transfer_money",
        "move_money",
        "change",
    )
    methods = [name for name, _ in inspect.getmembers(interfaces.ToolLayer, inspect.isfunction)]
    for name in methods:
        assert not any(word in name for word in forbidden), name
