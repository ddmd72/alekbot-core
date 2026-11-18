"""A message to a user's eager agent that an eviction unregistered rebuilds the user's agents (P15-S4).

`route_message` re-created only lazy recipients, so after `$admin_cache_reset` or the TTL sweep evicted a user mid-turn,
the turn's next delegation to an eager specialist ended in "Unknown recipient". The coordinator now asks the factory
port to restore the user's eager set. The pin (the real factory under a forced eviction) lives in
`tests/unit/composition/test_user_agent_factory_interleaving.py`.
"""
import logging
from types import SimpleNamespace

from src.domain.agent import AgentIntent, AgentMessage, AgentResponse, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_registry import AgentDescriptor, AgentRegistry, ExecutionMode
from src.ports.agent_factory_port import AgentFactoryPort

USER = "u1"


class _Agent:
    def __init__(self, agent_id: str) -> None:
        self.agent_id = agent_id
        self.agent_type = agent_id.rsplit("_", 1)[0]
        self.config = SimpleNamespace(capabilities=[])

    async def process(self, message) -> AgentResponse:
        return AgentResponse.success(task_id=message.task_id, agent_id=self.agent_id, result="answer")


class _Factory(AgentFactoryPort):
    """Registers the user's eager agent on restore, as the real factory's rebuild does; can be made to fail."""

    def __init__(self, coordinator: AgentCoordinator, fail: bool = False) -> None:
        self.coordinator = coordinator
        self.fail = fail
        self.lazy_requests: list = []

    async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
        self.lazy_requests.append(agent_type)
        return False

    async def restore_agents_for_user(self, user_id: str) -> bool:
        if self.fail:
            raise ValueError(f"User {user_id} not found")
        self.coordinator.register_agent(_Agent(f"web_agent_{user_id}"))
        return True


def _coordinator(fail: bool = False):
    registry = AgentRegistry()
    registry.register(AgentDescriptor(agent_id="web_agent", agent_type="web_agent", eager=True,
                                      capabilities={"do_web": ExecutionMode.SYNC}, description="Eager test agent"))
    coordinator = AgentCoordinator(registry=registry)
    factory = _Factory(coordinator, fail=fail)
    coordinator.set_agent_factory(factory)
    return coordinator, factory


def _message(recipient: str) -> AgentMessage:
    return AgentMessage.create(sender="coordinator", recipient=recipient, intent=AgentIntent.QUERY,
                               payload={"query": "q"}, context={"user_id": USER})


async def test_a_message_to_an_evicted_eager_agent_is_answered_by_the_rebuilt_agent():
    coordinator, factory = _coordinator()

    response = await coordinator.route_message(_message(f"web_agent_{USER}"))

    assert response.status == AgentStatus.SUCCESS
    assert response.agent_id == f"web_agent_{USER}"
    assert factory.lazy_requests == []  # an eager agent is restored with the user's set, never built as a lazy one


async def test_a_failed_rebuild_is_logged_and_the_message_is_refused_as_an_unknown_recipient(caplog):
    coordinator, _ = _coordinator(fail=True)

    with caplog.at_level(logging.ERROR):
        response = await coordinator.route_message(_message(f"web_agent_{USER}"))

    assert response.status == AgentStatus.FAILED
    assert response.error.startswith("Unknown recipient")
    assert "Eager agents rebuild failed" in caplog.text
    assert coordinator.get_agent(f"web_agent_{USER}") is None


async def test_a_factory_without_eager_agents_restores_nothing():
    class _LazyOnly(AgentFactoryPort):
        async def create_agent_on_demand(self, agent_type: str, user_id: str) -> bool:
            return False

    assert await _LazyOnly().restore_agents_for_user(USER) is False
