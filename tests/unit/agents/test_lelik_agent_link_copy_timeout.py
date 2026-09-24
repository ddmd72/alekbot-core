"""A hung notify_answer_copy must not hold up the spoken/returned delegation result
(VOICE_COMPANION_RFC §4.10 rule 2, M1)."""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

import src.agents.lelik_agent as lelik_module
from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.infrastructure.agent_coordinator import AgentCoordinator


@pytest.mark.asyncio
async def test_hung_chat_copy_does_not_block_delegate(monkeypatch):
    monkeypatch.setattr(lelik_module, "_ANSWER_COPY_TIMEOUT_S", 0.05)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    notifications = AsyncMock()
    notifications.notify_answer_copy.side_effect = hang
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=AsyncMock(), persona=persona, notifications=notifications,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="a", result="See https://example.com/x",
    ))
    agent.coordinator = coordinator

    output = await asyncio.wait_for(agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "q"}, call_context=[],
    ), timeout=1.0)

    assert "example.com" in output
    notifications.notify_answer_copy.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_is_logged_as_a_warning(monkeypatch):
    monkeypatch.setattr(lelik_module, "_ANSWER_COPY_TIMEOUT_S", 0.02)
    fake_logger = MagicMock()
    monkeypatch.setattr(lelik_module, "logger", fake_logger)

    async def hang(*_args, **_kwargs):
        await asyncio.sleep(10)

    notifications = AsyncMock()
    notifications.notify_answer_copy.side_effect = hang
    persona = AsyncMock()
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=AsyncMock(), persona=persona, notifications=notifications,
    )
    coordinator = MagicMock()
    coordinator.CALL_CHAIN_KEY = AgentCoordinator.CALL_CHAIN_KEY
    coordinator.handle_delegation = AsyncMock(return_value=AgentResponse.success(
        task_id="t", agent_id="a", result="See https://example.com/x",
    ))
    agent.coordinator = coordinator

    await asyncio.wait_for(agent.delegate(
        user_id="u1", account_id="a1",
        arguments={"intent": "search_memory", "query": "q"}, call_context=[],
    ), timeout=1.0)

    assert any("timed out" in c.args[0] for c in fake_logger.warning.call_args_list)
