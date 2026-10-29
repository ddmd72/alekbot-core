"""The Alek gateway labels the run it routes to Router → Smart (AGENT_WORKSPACE_RFC §9): a question is
voice:ask_alek, an errand (a Cloud Task, /worker) is voice:send_to_chat."""
import pytest

from tests.unit.agents.test_alek_gateway_agent import _agent, _message


@pytest.mark.parametrize("intent,origin", [("ask_alek", "voice:ask_alek"), ("send_to_chat", "voice:send_to_chat")])
async def test_run_origin(intent, origin):
    agent, coordinator, _ = _agent()
    await agent.execute(_message(intent=intent))
    routed = coordinator.route_message.await_args.args[0]
    assert routed.context["run_origin"] == origin


async def test_an_incoming_origin_is_replaced():
    agent, coordinator, _ = _agent()
    message = _message()
    message.context["run_origin"] = "chat"
    await agent.execute(message)
    assert coordinator.route_message.await_args.args[0].context["run_origin"] == "voice:ask_alek"
