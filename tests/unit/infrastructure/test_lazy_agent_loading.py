"""
Unit tests for lazy agent loading.

Verifies that:
1. AgentDescriptor.eager field defaults to True.
2. AgentCoordinator triggers lazy loading for non-eager agents.
3. AgentCoordinator._try_lazy_load extracts base_id and calls factory.
4. AgentFactoryPort is called with correct agent_type and user_id.
5. handle_delegation triggers _ensure_lazy_agent for non-eager descriptors.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentMessage, AgentResponse, AgentIntent
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentRegistry, AgentDescriptor, ExecutionMode
from src.ports.agent_factory_port import AgentFactoryPort


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def registry():
    return AgentRegistry()


@pytest.fixture
def mock_factory():
    factory = AsyncMock(spec=AgentFactoryPort)
    factory.create_agent_on_demand.return_value = True
    return factory


@pytest.fixture
def coordinator(registry, mock_factory):
    return AgentCoordinator(
        registry=registry,
        agent_factory=mock_factory,
    )


def _make_eager_descriptor(agent_id="eager_agent", intent="do_eager"):
    return AgentDescriptor(
        agent_id=agent_id,
        agent_type=agent_id,
        eager=True,
        capabilities={intent: ExecutionMode.SYNC},
        description="Eager test agent",
    )


def _make_lazy_descriptor(agent_id="lazy_agent", intent="do_lazy", agent_type=None):
    return AgentDescriptor(
        agent_id=agent_id,
        agent_type=agent_type or agent_id,
        eager=False,
        capabilities={intent: ExecutionMode.SYNC},
        description="Lazy test agent",
    )


def _make_mock_agent(agent_id: str):
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.process = AsyncMock(return_value=AgentResponse.success(
        task_id="test", agent_id=agent_id, result={"text": "ok"},
    ))
    return agent


# ---------------------------------------------------------------------------
# AgentDescriptor.eager field
# ---------------------------------------------------------------------------

class TestAgentDescriptorEager:
    def test_default_is_true(self):
        desc = AgentDescriptor(agent_id="test")
        assert desc.eager is True

    def test_can_set_false(self):
        desc = AgentDescriptor(agent_id="test", eager=False)
        assert desc.eager is False


# ---------------------------------------------------------------------------
# AgentRegistry.get_descriptor
# ---------------------------------------------------------------------------

class TestRegistryGetDescriptor:
    def test_returns_descriptor(self, registry):
        desc = _make_eager_descriptor()
        registry.register(desc)
        assert registry.get_descriptor("eager_agent") is desc

    def test_returns_none_for_unknown(self, registry):
        assert registry.get_descriptor("nonexistent") is None


# ---------------------------------------------------------------------------
# AgentCoordinator — handle_delegation triggers lazy loading
# ---------------------------------------------------------------------------

class TestHandleDelegationLazyLoading:
    async def test_calls_factory_for_non_eager(self, coordinator, registry, mock_factory):
        desc = _make_lazy_descriptor(agent_type="my_lazy_type")
        registry.register(desc)

        # Register a mock agent as if factory created it
        mock_agent = _make_mock_agent("lazy_agent_user123")
        coordinator.agents["lazy_agent_user123"] = mock_agent

        context = {"user_id": "user123", "account_id": "acc1"}
        await coordinator.handle_delegation(
            intent="do_lazy", query="test", context=context,
        )
        mock_factory.create_agent_on_demand.assert_awaited_once_with(
            "my_lazy_type", "user123",
        )

    async def test_does_not_call_factory_for_eager(self, coordinator, registry, mock_factory):
        desc = _make_eager_descriptor()
        registry.register(desc)

        mock_agent = _make_mock_agent("eager_agent_user123")
        coordinator.agents["eager_agent_user123"] = mock_agent

        context = {"user_id": "user123", "account_id": "acc1"}
        await coordinator.handle_delegation(
            intent="do_eager", query="test", context=context,
        )
        mock_factory.create_agent_on_demand.assert_not_awaited()


# ---------------------------------------------------------------------------
# AgentCoordinator._try_lazy_load (via route_message)
# ---------------------------------------------------------------------------

class TestRouteMessageLazyLoad:
    async def test_lazy_loads_unknown_recipient(self, coordinator, registry, mock_factory):
        desc = _make_lazy_descriptor(agent_id="doc_gen", agent_type="doc_generator")
        registry.register(desc)

        # Pre-register the agent so route_message finds it after lazy load
        mock_agent = _make_mock_agent("doc_gen_user456")
        mock_factory.create_agent_on_demand.side_effect = (
            lambda at, uid: coordinator.agents.update({"doc_gen_user456": mock_agent}) or True
        )

        msg = AgentMessage.create(
            sender="coordinator",
            recipient="doc_gen_user456",
            intent=AgentIntent.QUERY,
            payload={"query": "test"},
            context={"user_id": "user456"},
        )
        response = await coordinator.route_message(msg)
        mock_factory.create_agent_on_demand.assert_awaited_once_with(
            "doc_generator", "user456",
        )
        assert response.status.value == "success"

    async def test_no_lazy_load_for_eager_recipient(self, coordinator, registry, mock_factory):
        desc = _make_eager_descriptor(agent_id="web_agent")
        registry.register(desc)

        msg = AgentMessage.create(
            sender="test",
            recipient="web_agent_user789",
            intent=AgentIntent.QUERY,
            payload={},
            context={"user_id": "user789"},
        )
        # This will fail (agent not registered) but should NOT call factory
        await coordinator.route_message(msg)
        mock_factory.create_agent_on_demand.assert_not_awaited()

    async def test_no_lazy_load_without_user_id(self, coordinator, registry, mock_factory):
        desc = _make_lazy_descriptor()
        registry.register(desc)

        msg = AgentMessage.create(
            sender="test",
            recipient="lazy_agent_someone",
            intent=AgentIntent.QUERY,
            payload={},
            context={},  # no user_id
        )
        await coordinator.route_message(msg)
        mock_factory.create_agent_on_demand.assert_not_awaited()

    async def test_no_lazy_load_without_factory(self, registry):
        coordinator = AgentCoordinator(registry=registry, agent_factory=None)
        desc = _make_lazy_descriptor()
        registry.register(desc)

        msg = AgentMessage.create(
            sender="test",
            recipient="lazy_agent_user1",
            intent=AgentIntent.QUERY,
            payload={},
            context={"user_id": "user1"},
        )
        # Should not raise, just return error
        response = await coordinator.route_message(msg)
        assert response.status.value != "success"


# ---------------------------------------------------------------------------
# AgentFactoryPort contract
# ---------------------------------------------------------------------------

class TestAgentFactoryPortContract:
    def test_is_abstract(self):
        with pytest.raises(TypeError):
            AgentFactoryPort()

    def test_has_create_agent_on_demand(self):
        assert hasattr(AgentFactoryPort, "create_agent_on_demand")
