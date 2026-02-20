"""
Unit tests for LoggerAgent reliability (P1 fixes).

Covers:
- start() creates the flush task (not __init__)
- asyncio.Lock protects buffer under concurrent access
- _flush_logs drains buffer atomically and performs I/O outside lock
- shutdown flushes buffer before exit
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.agents.infrastructure.logger_agent import LoggerAgent
from src.domain.agent import AgentConfig, AgentMessage, AgentIntent
from src.config.environment import EnvironmentConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(agent_id="logger_agent"):
    return AgentConfig(
        agent_id=agent_id,
        agent_type="logger",
        timeout_ms=None,
        capabilities=[]
    )


def _make_message(level="INFO", text="test log"):
    return AgentMessage(
        intent=AgentIntent.INFORM,
        payload={"log_level": level, "message": text},
        sender="quick_response_agent",
        recipient="logger_agent",
        task_id="t1",
        context={"trace_id": "tr1", "session_id": "s1", "user_id": "u1"}
    )


def _make_agent(buffer_size=10, flush_interval=9999, log_sink=None):
    env_config = MagicMock(spec=EnvironmentConfig)
    env_config.is_development = True
    return LoggerAgent(
        config=_make_config(),
        env_config=env_config,
        log_sink=log_sink,
        buffer_size=buffer_size,
        flush_interval=flush_interval,
    )


# ---------------------------------------------------------------------------
# start() / __init__ task creation
# ---------------------------------------------------------------------------

class TestLoggerAgentLifecycle:

    def test_init_does_not_create_flush_task(self):
        """Background task must NOT be created in __init__ (no running loop yet)."""
        agent = _make_agent()
        assert agent._flush_task is None

    async def test_start_creates_flush_task(self):
        agent = _make_agent()
        await agent.start()

        assert agent._flush_task is not None
        assert not agent._flush_task.done()

        agent._flush_task.cancel()
        try:
            await agent._flush_task
        except asyncio.CancelledError:
            pass

    async def test_shutdown_cancels_flush_task(self):
        agent = _make_agent()
        await agent.start()
        task = agent._flush_task

        await agent.shutdown()

        assert task.cancelled() or task.done()

    async def test_shutdown_flushes_buffer(self):
        sink = MagicMock()
        agent = _make_agent(buffer_size=100, log_sink=sink)  # High threshold → no auto-flush
        # Add entries to buffer manually
        agent.buffer.append({"level": "INFO", "message": "pending", "agent": "x", "timestamp": 0,
                              "trace_id": None, "session_id": None, "user_id": None})

        await agent.shutdown()

        sink.log.assert_called_once()
        assert agent.buffer == []


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------

class TestLoggerAgentCanHandle:

    async def test_accepts_inform_with_required_fields(self):
        agent = _make_agent()
        assert await agent.can_handle(_make_message())

    async def test_rejects_non_inform_intent(self):
        agent = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.QUERY,
            payload={"log_level": "INFO", "message": "x"},
            sender="x", recipient="logger_agent", task_id="t", context={}
        )
        assert not await agent.can_handle(msg)

    async def test_rejects_missing_log_level(self):
        agent = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.INFORM,
            payload={"message": "x"},  # missing log_level
            sender="x", recipient="logger_agent", task_id="t", context={}
        )
        assert not await agent.can_handle(msg)

    async def test_rejects_missing_message(self):
        agent = _make_agent()
        msg = AgentMessage(
            intent=AgentIntent.INFORM,
            payload={"log_level": "INFO"},  # missing message
            sender="x", recipient="logger_agent", task_id="t", context={}
        )
        assert not await agent.can_handle(msg)


# ---------------------------------------------------------------------------
# execute + flush threshold
# ---------------------------------------------------------------------------

class TestLoggerAgentExecute:

    async def test_execute_accumulates_in_buffer(self):
        agent = _make_agent(buffer_size=10)
        await agent.execute(_make_message())
        assert len(agent.buffer) == 1

    async def test_execute_flushes_when_buffer_full(self):
        sink = MagicMock()
        agent = _make_agent(buffer_size=3, log_sink=sink)

        for i in range(3):
            await agent.execute(_make_message(text=f"msg {i}"))

        sink.log.assert_called()
        assert len(sink.log.call_args_list) == 3
        assert agent.buffer == []

    async def test_execute_returns_success(self):
        agent = _make_agent()
        response = await agent.execute(_make_message())
        assert response.success
        assert response.result == "logged"

    async def test_flush_uses_log_sink_when_provided(self):
        sink = MagicMock()
        agent = _make_agent(buffer_size=1, log_sink=sink)

        await agent.execute(_make_message(text="via sink"))

        sink.log.assert_called_once()
        logged = sink.log.call_args[0][0]
        assert logged["message"] == "via sink"

    async def test_flush_without_sink_falls_back_to_print(self, capsys):
        agent = _make_agent(buffer_size=1, log_sink=None)
        await agent.execute(_make_message(level="WARN", text="fallback"))

        captured = capsys.readouterr()
        assert "WARN" in captured.out
        assert "fallback" in captured.out


# ---------------------------------------------------------------------------
# Concurrency: asyncio.Lock protects buffer
# ---------------------------------------------------------------------------

class TestLoggerAgentConcurrency:

    async def test_concurrent_execute_does_not_corrupt_buffer(self):
        """
        50 concurrent execute calls → buffer has exactly 50 entries.
        No entries lost, no duplicates.
        """
        agent = _make_agent(buffer_size=1000)  # No auto-flush

        async def one_execute():
            await agent.execute(_make_message(text="concurrent"))

        await asyncio.gather(*[one_execute() for _ in range(50)])

        assert len(agent.buffer) == 50

    async def test_concurrent_flush_drains_buffer_exactly_once(self):
        """
        Two concurrent _flush_logs calls → combined entries flushed == entries added.
        Buffer must be empty and no entry lost or double-flushed.
        """
        flushed = []
        sink = MagicMock()
        sink.log = MagicMock(side_effect=lambda e: flushed.append(e))
        agent = _make_agent(buffer_size=1000, log_sink=sink)

        # Pre-fill buffer with 10 entries
        for i in range(10):
            agent.buffer.append({"level": "INFO", "message": f"m{i}", "agent": "x",
                                  "timestamp": 0, "trace_id": None, "session_id": None, "user_id": None})

        await asyncio.gather(
            agent._flush_logs(),
            agent._flush_logs(),
        )

        # Total flushed must be exactly 10 (no double-flush)
        assert len(flushed) == 10
        assert agent.buffer == []
