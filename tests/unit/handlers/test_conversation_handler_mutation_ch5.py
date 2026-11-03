"""Mutation survivors of the long-turn watcher/awaiter: each guarded step may fail without
stopping the later steps (RFC long turns §5.3-5.7)."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentStatus
from src.domain.messaging import MessageContext
from src.domain.settings import ConsolidationSettings
from src.domain.turn_clock import TurnClock
from src.handlers.conversation_handler import ConversationHandler, _LongTurn


def _context() -> MessageContext:
    return MessageContext(
        text="question", session_id="u:D1", user_id="alpha-user", account_id="a",
        attachments=[], metadata={"turn_id": "turn-1", "event_time": 100.1},
    )


@pytest.fixture
def env():
    handler = ConversationHandler(
        coordinator=MagicMock(), agent_factory=MagicMock(), file_service=MagicMock(),
        global_config=ConsolidationSettings(threshold=50, batch_size=30),
        fallback_service=MagicMock(), long_turn_service=MagicMock(),
    )
    long_turns = SimpleNamespace(
        notice_after_s=0.0, heartbeat_s=0.0,
        mark=AsyncMock(), heartbeat=AsyncMock(return_value=False),
    )
    channel = MagicMock()
    channel.update_message = AsyncMock()
    channel.on_long_turn = AsyncMock()
    return SimpleNamespace(handler=handler, long_turns=long_turns, channel=channel, stop=AsyncMock())


async def _watch(env, *, heartbeat_cancels: bool = False):
    """Run the watcher against a never-finishing agent task; return the _LongTurn."""
    agent_task = asyncio.create_task(asyncio.sleep(30))
    lt = _LongTurn(turn_id="turn-1", clock=TurnClock.start(), agent_task=agent_task)
    if heartbeat_cancels:
        env.long_turns.heartbeat = AsyncMock(return_value=True)
    else:
        async def beat(*a, **k):
            agent_task.cancel()   # ends the polling loop after one heartbeat
            return False
        env.long_turns.heartbeat = AsyncMock(side_effect=beat)
    try:
        await asyncio.wait_for(
            env.handler._watch_long_turn(
                lt, env.long_turns, _context(), env.channel, "status-1", env.stop, [], {},
            ),
            timeout=5,
        )
    finally:
        agent_task.cancel()
        await asyncio.gather(agent_task, return_exceptions=True)
    return lt


async def test_status_animation_error_does_not_stop_the_mark(env):
    env.stop.side_effect = RuntimeError("animation died")
    lt = await _watch(env)
    assert lt.marked is True
    env.channel.update_message.assert_awaited_once()
    env.long_turns.mark.assert_awaited_once()
    env.channel.on_long_turn.assert_awaited_once()


async def test_notice_update_error_still_marks_the_turn(env):
    env.channel.update_message.side_effect = RuntimeError("edit failed")
    lt = await _watch(env)
    assert lt.marked is True
    env.long_turns.mark.assert_awaited_once()
    env.channel.on_long_turn.assert_awaited_once()


async def test_mark_error_still_runs_the_long_turn_hook_and_polling(env):
    env.long_turns.mark.side_effect = RuntimeError("store down")
    lt = await _watch(env)
    assert lt.marked is True
    env.channel.on_long_turn.assert_awaited_once()
    env.long_turns.heartbeat.assert_awaited()


async def test_long_turn_hook_error_does_not_stop_cancel_polling(env):
    env.channel.on_long_turn.side_effect = RuntimeError("hook failed")
    lt = await _watch(env, heartbeat_cancels=True)
    env.long_turns.heartbeat.assert_awaited()
    assert lt.cancelled is True


async def test_user_cancel_becomes_cancelled_by_user_failure(env):
    agent_task = asyncio.create_task(asyncio.sleep(30))
    lt = _LongTurn(turn_id="turn-1", clock=TurnClock.start(), agent_task=agent_task)
    lt.cancelled = True
    message = SimpleNamespace(task_id="task-1")
    awaiting = asyncio.create_task(env.handler._await_agent_turn(
        lt, env.long_turns, message, _context(), env.channel, "status-1",
        env.stop, [], {},
    ))
    await asyncio.sleep(0)
    agent_task.cancel()
    response = await asyncio.wait_for(awaiting, timeout=5)
    assert response is not None
    assert response.status == AgentStatus.FAILED
    assert response.error == "cancelled_by_user"
    assert response.task_id == "task-1"
