"""Mutation-survivor tests (group F) for AgentCoordinator: gather kwarg, alert failure, lazy failure."""
from unittest.mock import AsyncMock, MagicMock, patch

from src.domain.agent import AgentMessage, AgentResponse, AgentStatus
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.ports.agent_factory_port import AgentFactoryPort


class _BrokenSink:
    async def post(self, text: str) -> None:
        raise RuntimeError("slack down")


def _msg(task_id: str) -> MagicMock:
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = task_id
    return msg


async def test_parallel_execute_forwards_return_exceptions_to_gather():
    coord = AgentCoordinator()

    async def boom(message):
        raise RuntimeError("route exploded")

    coord.route_message = boom  # type: ignore[method-assign]
    results = await coord.parallel_execute([_msg("t1")])  # default True: wrapped, not raised
    assert results[0].status == AgentStatus.FAILED
    assert "route exploded" in results[0].error


async def test_a_failing_alert_sink_does_not_stop_the_cycle_refusal():
    coord = AgentCoordinator(alert_sink=_BrokenSink())
    response = await coord._refuse_if_looping("agent_a", ["agent_a"], "caller")
    assert response is not None
    assert response.status == AgentStatus.FAILED
    assert "Delegation cycle refused" in response.error


async def test_a_failing_lazy_instantiation_is_logged_as_an_error():
    factory = AsyncMock(spec=AgentFactoryPort)
    factory.create_agent_on_demand.side_effect = RuntimeError("factory down")
    coord = AgentCoordinator(agent_factory=factory)
    with patch("src.infrastructure.agent_coordinator.logger") as log:
        await coord._ensure_lazy_agent("doc_generator", {"user_id": "user12345"})
    log.error.assert_called_once()
    assert "factory down" in str(log.error.call_args.args)
