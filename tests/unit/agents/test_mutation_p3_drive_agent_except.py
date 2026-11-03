"""DriveAgent's two `except Exception` handlers keep their work (mutation P3 survivors, drive_agent L277 and L305).

L277: an engine that dies mid-run still yields the code-built answer, marked cut, and the failure is logged.
L305: a failing turn-cap alert is logged as a warning and never changes the answer."""
import logging
from unittest.mock import AsyncMock

import pytest

from src.agents import drive_agent as module
from src.domain.agent import AgentStatus
from tests.support.scripted_llm import script
from tests.unit.agents.test_drive_agent import _agent, _msg, _stalled, ops, prompts  # noqa: F401


async def test_an_engine_that_raises_still_answers_cut_and_logs_the_failure(ops, prompts, monkeypatch, caplog):
    async def boom(self, *args, **kwargs):
        raise RuntimeError("engine exploded")
    monkeypatch.setattr(module.DelegationEngine, "execute", boom)
    agent = _agent(ops, prompts)
    agent._call_llm, _ = script()

    with caplog.at_level(logging.WARNING):
        resp = await agent.execute(_msg())

    assert resp is not None and resp.status == AgentStatus.SUCCESS
    assert resp.metadata["drive_status"] == "unfinished"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("engine ended with RuntimeError: engine exploded" in w for w in warnings)


async def test_a_failing_turn_cap_alert_is_logged_as_a_warning_and_the_answer_stands(ops, prompts, caplog):
    sink = AsyncMock()
    sink.post.side_effect = RuntimeError("slack down")
    agent = _agent(ops, prompts, alert_sink=sink)
    agent._call_llm, _ = script(*_stalled())

    with caplog.at_level(logging.WARNING):
        resp = await agent.execute(_msg())

    assert resp.status == AgentStatus.SUCCESS and resp.metadata["drive_status"] == "unfinished"
    sink.post.assert_awaited_once()
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("ops alert delivery failed: slack down" in w for w in warnings)
