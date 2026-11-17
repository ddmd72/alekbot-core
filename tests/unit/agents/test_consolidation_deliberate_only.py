"""The consolidate task runs only the deliberate process; without a fact management port it fails loud (no fallback)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.agent import AgentStatus
from src.domain.request_context import RequestContext
from src.ports.fact_management_port import FactManagementPort
from tests.unit.agents.test_consolidation_agent import _ACCOUNT_ID, _USER_ID, _make_agent, _make_message


async def _execute(agent, message):
    with RequestContext(user_id=_USER_ID, account_id=_ACCOUNT_ID):
        return await agent.execute(message)


async def test_consolidate_without_a_fact_management_port_fails_logs_and_touches_nothing(monkeypatch):
    agent, _, _, pb = _make_agent(fact_management=None)
    deliberate = AsyncMock(return_value={"operations": []})
    monkeypatch.setattr(agent, "_execute_deliberate_process_v3", deliberate)
    errors = []
    monkeypatch.setattr("src.agents.consolidation_agent.logger.error", lambda msg, *a, **k: errors.append(str(msg)))

    response = await _execute(agent, _make_message("consolidate", {"messages": [{"role": "user", "text": "hi"}]}))

    assert response.status == AgentStatus.FAILED
    assert response.error == "Consolidation needs the fact management port; none is wired"
    assert len(errors) == 1 and "no fact management port is wired" in errors[0] and _ACCOUNT_ID[:8] in errors[0]
    deliberate.assert_not_awaited()
    agent._llm.generate_content.assert_not_awaited()
    agent._bio_cache.refresh_cache.assert_not_awaited()
    pb.invalidate_biographical_cache.assert_not_called()


@pytest.mark.parametrize("task", ["consolidate", "synthesize", "librarian"])
@pytest.mark.parametrize("messages", [[], [{"role": "user", "text": "I moved to Springfield"}]], ids=["empty", "one"])
async def test_consolidate_always_runs_the_deliberate_process(monkeypatch, task, messages):
    agent, _, _, pb = _make_agent(fact_management=AsyncMock(spec=FactManagementPort))
    ops = [{"action": "CREATE", "fact_id": "fact-1"}, {"action": "DISCARD"}]
    deliberate = AsyncMock(return_value={"operations": ops})
    monkeypatch.setattr(agent, "_execute_deliberate_process_v3", deliberate)
    bio = [{"text": "lives in Springfield"}]

    response = await _execute(
        agent, _make_message(task, {"messages": messages, "biographical_context": bio}, {"owner_timezone": "Europe/Lisbon"}))

    assert response.status == AgentStatus.SUCCESS, response.error
    deliberate.assert_awaited_once_with(
        messages=messages, user_id=_USER_ID, account_id=_ACCOUNT_ID, bio_context_raw=bio, owner_timezone="Europe/Lisbon")
    assert response.result == {"operations": 2, "facts_affected": 1, "message": "Processed 2 operations"}
    assert response.metadata["operations"] == ops and response.metadata["version"] == "v3"
    agent._bio_cache.refresh_cache.assert_awaited_once_with(
        owner_id=_ACCOUNT_ID, facts_limit=agent.facts_limit, principles_limit=agent.principles_limit)
    pb.invalidate_biographical_cache.assert_called_once_with(_ACCOUNT_ID)


async def test_a_failed_biographical_cache_read_is_swallowed_and_the_deliberate_process_runs_without_context(monkeypatch):
    agent, repo, _, _ = _make_agent(fact_management=AsyncMock(spec=FactManagementPort))
    repo.get_biographical_context_cached = AsyncMock(side_effect=RuntimeError("cache error"))
    deliberate = AsyncMock(return_value={"operations": []})
    monkeypatch.setattr(agent, "_execute_deliberate_process_v3", deliberate)

    response = await _execute(agent, _make_message("consolidate", {"messages": []}))

    assert response.status == AgentStatus.SUCCESS, response.error
    repo.get_biographical_context_cached.assert_awaited_once()
    assert deliberate.await_args.kwargs["bio_context_raw"] == []
