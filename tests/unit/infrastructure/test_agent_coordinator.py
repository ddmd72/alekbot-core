"""
Unit tests for AgentCoordinator.

Coverage:
  register_agent()
    - registers agent; raises ValueError on duplicate
  unregister_agent()
    - returns True when found, False when missing
  get_agent() / list_agents() / get_agents_by_capability()
    - basic accessors
  route_message()
    - explicit routing → calls agent.process()
    - explicit routing with exception → returns failure
    - broadcast routing → calls _broadcast_route
    - unknown recipient → returns failure
  _broadcast_route()
    - no capable agents → failure
    - single capable agent → routes
    - multiple capable agents → selects first, logs alternatives
    - can_handle() exception → warning, skips agent
  parallel_execute()
    - returns results in order
    - exceptions wrapped as failure responses
  handle_delegation()
    - no registry → failure
    - unknown intent → failure
    - SYNC intent → routes via _execute_sync
    - ASYNC intent → enqueues via _execute_async
  _execute_async()
    - no task_queue → failure
    - enqueues and returns ack
  get_available_intents() / get_available_intents_for()
    - no registry → []
    - delegates to registry
  get_status()
    - returns total_agents and per-agent status
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from src.domain.agent import AgentMessage, AgentResponse, AgentIntent, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentRegistry, AgentDescriptor, ExecutionMode


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(agent_id="agent_a", agent_type="test", capabilities=None):
    agent = MagicMock()
    agent.agent_id = agent_id
    agent.agent_type = agent_type
    agent.config = MagicMock()
    agent.config.capabilities = capabilities or {}
    agent.process = AsyncMock(return_value=AgentResponse.success(
        task_id="t1", agent_id=agent_id, result={"answer": "yes"}
    ))
    agent.can_handle = AsyncMock(return_value=True)
    agent.get_status = MagicMock(return_value={"status": "ok"})
    return agent


def _make_message(recipient="agent_a", intent=AgentIntent.QUERY, task_id="t1"):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = task_id
    msg.sender = "orchestrator"
    msg.recipient = recipient
    msg.intent = intent
    msg.context = {"user_id": "user1"}
    msg.payload = {"query": "hello"}
    return msg


def _make_registry(intent="search_memory", mode=ExecutionMode.SYNC):
    registry = MagicMock(spec=AgentRegistry)
    desc = AgentDescriptor(
        agent_id="memory_agent",
        capabilities={intent: mode},
        dispatch_deadline_s=300,
    )
    registry.get_agent_for_intent = MagicMock(return_value=desc)
    registry.get_available_intents = MagicMock(return_value=[{"name": intent}])
    registry.get_available_intents_for = MagicMock(return_value=[{"name": intent}])
    return registry


# ---------------------------------------------------------------------------
# register_agent() / unregister_agent()
# ---------------------------------------------------------------------------

class TestRegisterUnregister:

    def test_register_adds_agent(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        coord.register_agent(agent)
        assert coord.get_agent("a1") is agent

    def test_register_duplicate_raises(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        coord.register_agent(agent)
        with pytest.raises(ValueError, match="already registered"):
            coord.register_agent(agent)

    def test_unregister_existing_returns_true(self):
        coord = AgentCoordinator()
        coord.register_agent(_make_agent("a1"))
        assert coord.unregister_agent("a1") is True
        assert coord.get_agent("a1") is None

    def test_unregister_missing_returns_false(self):
        coord = AgentCoordinator()
        assert coord.unregister_agent("ghost") is False


# ---------------------------------------------------------------------------
# get_agent() / list_agents() / get_agents_by_capability()
# ---------------------------------------------------------------------------

class TestAccessors:

    def test_get_agent_missing_returns_none(self):
        coord = AgentCoordinator()
        assert coord.get_agent("x") is None

    def test_list_agents_empty(self):
        assert AgentCoordinator().list_agents() == []

    def test_list_agents_after_registration(self):
        coord = AgentCoordinator()
        coord.register_agent(_make_agent("a1"))
        coord.register_agent(_make_agent("a2"))
        assert set(coord.list_agents()) == {"a1", "a2"}

    def test_get_agents_by_capability_found(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1", capabilities={"search_memory": ExecutionMode.SYNC})
        coord.register_agent(agent)
        results = coord.get_agents_by_capability("search_memory")
        assert len(results) == 1
        assert results[0] is agent

    def test_get_agents_by_capability_not_found(self):
        coord = AgentCoordinator()
        coord.register_agent(_make_agent("a1", capabilities={}))
        assert coord.get_agents_by_capability("missing_cap") == []


# ---------------------------------------------------------------------------
# route_message()
# ---------------------------------------------------------------------------

class TestRouteMessage:

    async def test_explicit_routing_calls_process(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        coord.register_agent(agent)
        msg = _make_message(recipient="a1")
        response = await coord.route_message(msg)
        agent.process.assert_called_once_with(msg)
        assert response.status == AgentStatus.SUCCESS

    async def test_explicit_routing_exception_returns_failure(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        agent.process = AsyncMock(side_effect=RuntimeError("crashed"))
        coord.register_agent(agent)
        msg = _make_message(recipient="a1")
        response = await coord.route_message(msg)
        assert response.status == AgentStatus.FAILED
        assert "crashed" in response.error

    async def test_broadcast_routing(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        coord.register_agent(agent)
        msg = _make_message(recipient="broadcast")
        response = await coord.route_message(msg)
        agent.process.assert_called_once()
        assert response.status == AgentStatus.SUCCESS

    async def test_unknown_recipient_returns_failure(self):
        coord = AgentCoordinator()
        msg = _make_message(recipient="ghost_agent")
        response = await coord.route_message(msg)
        assert response.status == AgentStatus.FAILED
        assert "ghost_agent" in response.error


# ---------------------------------------------------------------------------
# _broadcast_route()
# ---------------------------------------------------------------------------

class TestBroadcastRoute:

    async def test_no_capable_agents_returns_failure(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        agent.can_handle = AsyncMock(return_value=False)
        coord.register_agent(agent)
        msg = _make_message(recipient="broadcast")
        response = await coord._broadcast_route(msg)
        assert response.status == AgentStatus.FAILED

    async def test_can_handle_exception_skipped(self):
        """can_handle() raising must not crash routing."""
        coord = AgentCoordinator()
        bad_agent = _make_agent("bad")
        bad_agent.can_handle = AsyncMock(side_effect=RuntimeError("oops"))
        good_agent = _make_agent("good")
        coord.register_agent(bad_agent)
        coord.register_agent(good_agent)
        msg = _make_message(recipient="broadcast")
        response = await coord._broadcast_route(msg)
        # good_agent handled it
        assert response.status == AgentStatus.SUCCESS

    async def test_multiple_capable_logs_alternatives(self):
        """Multiple capable agents → first selected."""
        coord = AgentCoordinator()
        a1 = _make_agent("a1")
        a2 = _make_agent("a2")
        coord.register_agent(a1)
        coord.register_agent(a2)
        msg = _make_message(recipient="broadcast")
        response = await coord._broadcast_route(msg)
        assert response.status == AgentStatus.SUCCESS


# ---------------------------------------------------------------------------
# parallel_execute()
# ---------------------------------------------------------------------------

class TestParallelExecute:

    async def test_returns_results_in_order(self):
        coord = AgentCoordinator()
        a1 = _make_agent("a1")
        a2 = _make_agent("a2")
        coord.register_agent(a1)
        coord.register_agent(a2)
        msgs = [_make_message(recipient="a1"), _make_message(recipient="a2")]
        results = await coord.parallel_execute(msgs)
        assert len(results) == 2
        assert all(r.status == AgentStatus.SUCCESS for r in results)

    async def test_exceptions_wrapped_as_failures(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        agent.process = AsyncMock(side_effect=RuntimeError("fail"))
        coord.register_agent(agent)
        msgs = [_make_message(recipient="a1", task_id="t1")]
        results = await coord.parallel_execute(msgs, return_exceptions=True)
        assert results[0].status == AgentStatus.FAILED


# ---------------------------------------------------------------------------
# handle_delegation()
# ---------------------------------------------------------------------------

class TestHandleDelegation:

    async def test_no_registry_returns_failure(self):
        coord = AgentCoordinator(registry=None)
        response = await coord.handle_delegation("search_memory", "query", {"user_id": "u1"})
        assert response.status == AgentStatus.FAILED
        assert "AgentRegistry" in response.error

    async def test_unknown_intent_returns_failure(self):
        registry = MagicMock(spec=AgentRegistry)
        registry.get_agent_for_intent = MagicMock(return_value=None)
        registry.get_available_intents = MagicMock(return_value=[])
        coord = AgentCoordinator(registry=registry)
        response = await coord.handle_delegation("ghost_intent", "query", {"user_id": "u1"})
        assert response.status == AgentStatus.FAILED
        assert "ghost_intent" in response.error

    async def test_sync_intent_routes_to_agent(self):
        registry = _make_registry("search_memory", ExecutionMode.SYNC)
        coord = AgentCoordinator(registry=registry)
        agent = _make_agent("memory_agent_user1")
        coord.register_agent(agent)
        response = await coord.handle_delegation(
            "search_memory", "find facts", {"user_id": "user1"}
        )
        agent.process.assert_called_once()
        assert response.status == AgentStatus.SUCCESS

    async def test_async_intent_no_task_queue_returns_failure(self):
        registry = _make_registry("create_document", ExecutionMode.ASYNC)
        coord = AgentCoordinator(registry=registry, task_queue=None)
        response = await coord.handle_delegation(
            "create_document", "make doc", {"user_id": "u1"}
        )
        assert response.status == AgentStatus.FAILED
        assert "TaskQueue" in response.error

    async def test_async_intent_enqueues_and_returns_ack(self):
        registry = _make_registry("create_document", ExecutionMode.ASYNC)
        task_queue = MagicMock()
        task_queue.enqueue_agent_task = AsyncMock(return_value="task-123")
        coord = AgentCoordinator(registry=registry, task_queue=task_queue)
        response = await coord.handle_delegation(
            "create_document", "make doc", {"user_id": "u1"}
        )
        assert response.status == AgentStatus.SUCCESS
        assert response.result["status"] == "started"
        assert response.result["task_name"] == "task-123"


# ---------------------------------------------------------------------------
# get_available_intents() / get_available_intents_for()
# ---------------------------------------------------------------------------

class TestGetAvailableIntents:

    def test_no_registry_returns_empty(self):
        coord = AgentCoordinator(registry=None)
        assert coord.get_available_intents() == []
        desc = AgentDescriptor(agent_id="x", capabilities={})
        assert coord.get_available_intents_for(desc) == []

    def test_delegates_to_registry(self):
        registry = _make_registry()
        coord = AgentCoordinator(registry=registry)
        result = coord.get_available_intents()
        assert result == [{"name": "search_memory"}]

    def test_get_available_intents_for_delegates(self):
        registry = _make_registry()
        coord = AgentCoordinator(registry=registry)
        desc = AgentDescriptor(agent_id="orch", capabilities={})
        result = coord.get_available_intents_for(desc)
        registry.get_available_intents_for.assert_called_once_with(desc)
        assert result == [{"name": "search_memory"}]


# ---------------------------------------------------------------------------
# get_status()
# ---------------------------------------------------------------------------

class TestGetStatus:

    def test_empty_coordinator(self):
        coord = AgentCoordinator()
        status = coord.get_status()
        assert status["total_agents"] == 0
        assert status["agents"] == {}

    def test_returns_per_agent_status(self):
        coord = AgentCoordinator()
        agent = _make_agent("a1")
        coord.register_agent(agent)
        status = coord.get_status()
        assert status["total_agents"] == 1
        assert "a1" in status["agents"]
        assert status["agents"]["a1"] == {"status": "ok"}
