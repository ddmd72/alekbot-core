"""ConsolidationAgent callers of get_biographical_context_cached degrade on a backend error (facts-fix PR).

The adapter now raises on a backend failure; the keep-alive touch between LLM turns must never abort a pass, and the
anchors read degrades to no anchors.
"""

import json
from unittest.mock import AsyncMock

from src.ports.llm_port import LLMResponse, ToolCall
from tests.unit.agents.test_consolidation_agent import _ACCOUNT_ID, _USER_ID, _make_agent


async def test_anchors_read_degrades_to_empty_with_a_warning(monkeypatch):
    agent, repo, _, _ = _make_agent()
    repo.get_biographical_context_cached = AsyncMock(side_effect=RuntimeError("backend down"))
    warnings = []
    monkeypatch.setattr("src.agents.consolidation_agent.logger.warning", lambda msg, *a, **k: warnings.append(str(msg)))

    assert await agent._get_existing_anchors_list(_ACCOUNT_ID) == []
    assert any("backend down" in w for w in warnings)


async def test_keep_alive_touch_failure_does_not_abort_the_pass(monkeypatch):
    first = LLMResponse(text=None, tool_calls=[ToolCall(name="count_words", args={"text": "hello world"})])
    final = LLMResponse(text=json.dumps({"operations": [{"action": "CREATE"}]}), tool_calls=[])
    mock_llm = AsyncMock(side_effect=[first, final])
    agent, repo, _, _ = _make_agent(mock_llm=mock_llm)
    repo.get_biographical_context_cached = AsyncMock(side_effect=RuntimeError("backend down"))
    warnings = []
    monkeypatch.setattr("src.agents.consolidation_agent.logger.warning", lambda msg, *a, **k: warnings.append(str(msg)))

    result = await agent._run_consolidation_loop(
        user_message_text="begin", system_prompt="SYS", user_id=_USER_ID, account_id=_ACCOUNT_ID,
        write_context=agent._write_context("stage1"),
    )

    assert result == [{"action": "CREATE"}]
    assert repo.get_biographical_context_cached.await_count == 1
    assert any("backend down" in w for w in warnings)
