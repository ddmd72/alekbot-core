"""SmartResponseAgent: two error branches of the run body.

  - A terminal `TranscriptLockedError` raised inside the run propagates out of `_run` unchanged: `execute()` owns the
    provider rotation, so converting it to `AgentResponse.failure` here would end the rotation before it starts.
  - A failing long-turn registry means "no other running jobs", never a failed turn."""
import logging
from unittest.mock import AsyncMock, patch

import pytest

from src.domain.exceptions import TranscriptLockedError
from src.infrastructure.delegation_engine import DelegationEngine

from .test_smart_turn_clock import (  # noqa: F401  (fixtures + helpers)
    _make_ctx,
    _make_message,
    _make_smart,
    smart_agent_with_engine_spy,
)


async def test_a_transcript_lock_inside_the_run_propagates_instead_of_becoming_a_failure():
    agent = _make_smart(_make_ctx())
    message = _make_message()
    locked = TranscriptLockedError(provider_name="stub", cause=Exception("529 overloaded"), turn=2)
    agent._on_agent_error = AsyncMock()

    async def _raising_execute(self, **kwargs):
        raise locked

    with patch.object(DelegationEngine, "execute", _raising_execute):
        with pytest.raises(TranscriptLockedError) as raised:
            await agent._run(message, agent._resolve_effective(message))

    assert raised.value is locked
    agent._on_agent_error.assert_not_called()  # the error is execute()'s to account for, after rotation fails


async def test_an_ordinary_error_inside_the_run_still_becomes_a_failure_response():
    agent = _make_smart(_make_ctx())
    message = _make_message()

    async def _raising_execute(self, **kwargs):
        raise RuntimeError("engine blew up")

    with patch.object(DelegationEngine, "execute", _raising_execute):
        response = await agent._run(message, agent._resolve_effective(message))

    assert response.status.value == "failed"
    assert "Smart response failed: engine blew up" in response.error


async def test_a_registry_failure_means_no_running_jobs(caplog):
    agent = _make_smart(_make_ctx())
    agent.long_turn_registry = AsyncMock()
    agent.long_turn_registry.list_running.side_effect = RuntimeError("firestore unavailable")

    with caplog.at_level(logging.WARNING):
        jobs = await agent._load_running_jobs("user-123", _make_message())

    assert jobs == []
    agent.long_turn_registry.list_running.assert_awaited_once_with("user-123")
    assert any("running jobs unavailable" in r.getMessage() for r in caplog.records)


async def test_a_registry_failure_does_not_fail_the_turn_or_offer_the_cancel_tool(smart_agent_with_engine_spy):
    agent, engine_kwargs, _ = smart_agent_with_engine_spy
    agent.long_turn_registry = AsyncMock()
    agent.long_turn_registry.list_running.side_effect = RuntimeError("firestore unavailable")

    response = await agent.execute(smart_agent_with_engine_spy.message)

    assert response.status.value == "success"
    assert "cancel_long_turn" not in (engine_kwargs.get("local_tools") or {})
