"""Mutation-survivor kills for TasksAgent: time-budget handling, the final-answer request and the page argument."""
import asyncio
from unittest.mock import MagicMock

from src.domain.agent import AgentIntent, AgentMessage, AgentStatus
from src.ports.llm_port import LLMResponse
from tests.unit.agents.test_tasks_agent import (
    _USER_ID, _llm_responses, _make_agent, _make_task,
)


def _message(timeout_ms):
    return AgentMessage(
        task_id="task-abc", sender="orch", recipient="tasks_agent", intent=AgentIntent.QUERY,
        payload={"query": "list my tasks"}, context={"user_id": _USER_ID, "account_id": "acc-1"},
        timeout_ms=timeout_ms,
    )


def _text(value):
    resp = MagicMock(spec=LLMResponse)
    resp.tool_calls = []
    resp.text = value
    resp.raw_content = None
    resp.finish_reason = None
    return resp


async def _run_with_a_hung_second_turn():
    """Turn 1 runs a tool; turn 2 hangs past the tool budget; the final answer turn then answers."""
    agent, provider, _ = _make_agent()
    provider.list_tasks.return_value = [_make_task()]
    tool_resp = _llm_responses("list_tasks", {}, "")[0]
    seen = []

    async def fake_call(request, turn=0, **_kw):
        seen.append((request, turn))
        if len(seen) == 1:
            return tool_resp
        if len(seen) == 2:
            await asyncio.sleep(5)
        return _text("Final summary.")

    agent._call_llm = fake_call
    agent._monotonic = lambda: 0.0  # frozen clock: the top-of-loop budget check never trips on a slow worker
    # Tool budget = timeout - 8 s answer reserve = 0.2 s of real time for the hung turn.
    response = await agent.execute(_message(timeout_ms=8200))
    return response, seen


async def test_a_tool_turn_that_runs_past_the_budget_falls_through_to_the_final_answer():
    response, seen = await _run_with_a_hung_second_turn()

    assert response is not None and response.status == AgentStatus.SUCCESS
    assert response.result == "Final summary."
    assert len(seen) == 3 and seen[2][0].tools == []


async def test_the_final_answer_request_carries_the_system_prompt_and_its_turn_number():
    _, seen = await _run_with_a_hung_second_turn()

    final_request, final_turn = seen[2]
    assert final_request.system_instruction == "system prompt"
    assert final_turn == 2   # one completed tool turn, so the final answer is turn tool_turns + 1


async def test_an_unreadable_page_argument_lists_the_first_page():
    agent, provider, _ = _make_agent()
    provider.list_tasks.return_value = [_make_task()]

    result = await agent._execute_tool("list_tasks", {"page": "next"}, _USER_ID)

    assert result["page"] == 1 and result["count"] == 1
