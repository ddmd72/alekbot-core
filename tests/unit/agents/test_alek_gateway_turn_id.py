"""The call path's turn id (DRIVE_FILE_NOTES_RFC §4.2, §4.9; core contract §6): LelikAgent fixes one per delegation,
the Alek gateway keeps it on the run it routes to Router → Smart — on ask_alek and on send_to_chat (whose Cloud Task
carries the delegation context, so a redelivery sees the same id) — and fills its own message's id only when none
came."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.turn_identity import TURN_ID_CONTEXT_KEY
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from tests.unit.agents.test_alek_gateway_agent import _agent, _message


@pytest.mark.parametrize("intent", ["ask_alek", "send_to_chat"])
async def test_the_delegation_turn_id_is_kept(intent):
    agent, coordinator, _ = _agent()
    message = _message(intent=intent)
    message.context[TURN_ID_CONTEXT_KEY] = "voice:abc"
    await agent.execute(message)
    assert coordinator.route_message.await_args.args[0].context[TURN_ID_CONTEXT_KEY] == "voice:abc"


@pytest.mark.parametrize("intent,origin", [("ask_alek", "voice:ask_alek"), ("send_to_chat", "voice:send_to_chat")])
async def test_without_one_the_message_id_is_fixed(intent, origin):
    agent, coordinator, _ = _agent()
    message = _message(intent=intent)
    await agent.execute(message)
    assert coordinator.route_message.await_args.args[0].context[TURN_ID_CONTEXT_KEY] == f"{origin}:{message.task_id}"


def _lelik():
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    agent = LelikAgent(config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(), from_number="+1",
                       status_callback_url="https://x/voice/status", prompt_builder=AsyncMock(), persona=persona,
                       notifications=AsyncMock(), voice_profile=VOICE_PROVIDER_PROFILES["openai"])
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(task_id="t", agent_id="a",
                                                                                 result="ok"))
    agent.coordinator = coordinator
    return agent, coordinator


async def test_lelik_fixes_one_turn_id_per_delegation():
    agent, coordinator = _lelik()
    args = {"intent": "send_to_chat", "query": "note the PDF from yesterday as the contractor's invoice"}
    await agent.delegate_outcome(user_id="u1", account_id="a1", arguments=args, call_context=[])
    await agent.delegate_outcome(user_id="u1", account_id="a1", arguments=args, call_context=[])
    first, second = (c.kwargs["context"][TURN_ID_CONTEXT_KEY] for c in coordinator.handle_delegation.await_args_list)
    assert first.startswith("voice:") and second.startswith("voice:") and first != second
