"""LelikAgent as a standard agent: own prompt, shared tool, engine dispatch (RFC §4.4/§4.7)."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.lelik_context import LelikContext
from src.domain.notification import NotificationChannel
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import LELIK


def _agent(channel=None, available=None):
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(
        biographical_facts=[{"domain": "work", "text": "f"}],
        conversation_history=[{"role": "user", "content": "hi"}],
    )
    persona.primary_channel.return_value = channel
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.get_available_intents_for.return_value = available if available is not None else [
        {"name": "search_memory", "description": "memory"},
        {"name": "search_web", "description": "web"},
    ]
    coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="a", result="sunny, 24°C"),
    )
    agent.coordinator = coordinator
    return agent, prompt_builder, persona, coordinator


@pytest.mark.asyncio
async def test_session_config_builds_the_lelik_prompt_over_persona_context():
    agent, prompt_builder, persona, _ = _agent()
    config = await agent.session_config(user_id="u1", account_id="a1")
    assert config["instructions"] == "PROMPT"
    persona.assemble.assert_awaited_once_with("u1", "a1")
    kwargs = prompt_builder.build_for_agent.await_args.kwargs
    assert kwargs["agent_type"] == "lelik"
    assert kwargs["user_id"] == "u1" and kwargs["account_id"] == "a1"
    assert kwargs["biographical_facts"] == [{"domain": "work", "text": "f"}]
    assert kwargs["conversation_history"] == [{"role": "user", "content": "hi"}]
    assert kwargs["include_biographical"] is True
    assert kwargs["include_directives"] is True
    assert kwargs["include_datetime"] is True


@pytest.mark.asyncio
async def test_session_config_carries_the_shared_delegate_tool_over_lelik_allowlist():
    agent, _, _, coordinator = _agent()
    config = await agent.session_config(user_id="u1", account_id="a1")
    coordinator.get_available_intents_for.assert_called_once_with(LELIK)
    [tool] = config["tools"]
    assert tool["name"] == "delegate_to_specialist"
    assert "- search_memory: memory" in tool["description"]
    assert "- search_web: web" in tool["description"]


@pytest.mark.asyncio
async def test_no_available_intents_means_no_tools():
    agent, *_ = _agent(available=[])
    assert (await agent.session_config(user_id="u1", account_id="a1"))["tools"] == []


@pytest.mark.asyncio
async def test_prompt_failure_propagates_so_the_webhook_fails_closed():
    agent, prompt_builder, *_ = _agent()
    prompt_builder.build_for_agent.side_effect = KeyError("Blueprint not found: lelik_agent_v1")
    with pytest.raises(KeyError):
        await agent.session_config(user_id="u1", account_id="a1")


@pytest.mark.asyncio
async def test_delegate_seeds_identity_primary_session_and_call_chain():
    channel = NotificationChannel(user_id="u1", platform="slack", channel_id="D9", updated_at=datetime.now(timezone.utc))
    agent, _, _, coordinator = _agent(channel=channel)
    output = await agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_web", "query": "weather Valencia"},
        call_context=[{"role": "user", "text": "what's the weather"}],
    )
    first = coordinator.handle_delegation.await_args_list[0].kwargs
    ctx = first["context"]
    assert ctx["user_id"] == "u1" and ctx["account_id"] == "a1"
    assert ctx["_call_chain"] == ["lelik_agent"]
    assert ctx["session_id"] == "u1:D9"
    assert ctx["origin_channel_id"] == "D9" and ctx["origin_platform"] == "slack"
    assert ctx["params"]["call_context"] == [{"role": "user", "text": "what's the weather"}]
    assert first["calling_agent_id"] == "lelik_agent_u1"
    # search_web fans out to maps through LELIK's own descriptor
    assert {c.kwargs["intent"] for c in coordinator.handle_delegation.await_args_list} == {"search_web", "maps_query"}
    assert "sunny, 24°C" in output


@pytest.mark.asyncio
async def test_delegate_without_a_channel_sends_no_session():
    agent, _, _, coordinator = _agent(channel=None)
    await agent.delegate(user_id="u1", account_id="a1",
                         arguments={"intent": "search_memory", "query": "q"}, call_context=[])
    ctx = coordinator.handle_delegation.await_args.kwargs["context"]
    assert "session_id" not in ctx and "origin_channel_id" not in ctx


@pytest.mark.asyncio
async def test_free_text_context_is_kept_as_reasoning_next_to_call_context():
    agent, _, _, coordinator = _agent()
    await agent.delegate(user_id="u1", account_id="a1",
                         arguments={"intent": "search_memory", "query": "q", "context": "caller asked twice"},
                         call_context=[])
    params = coordinator.handle_delegation.await_args.kwargs["context"]["params"]
    assert params == {"reasoning": "caller asked twice", "call_context": []}


@pytest.mark.asyncio
async def test_mode_is_stripped_so_lelik_always_runs_the_declared_mode():
    """'later' has no delivery path for SYNC-declared intents: the answer would be lost."""
    agent, _, _, coordinator = _agent()
    await agent.delegate(user_id="u1", account_id="a1",
                         arguments={"intent": "search_memory", "query": "q", "mode": "later"}, call_context=[])
    assert coordinator.handle_delegation.await_args.kwargs["mode_override"] is None
