"""Quick ends on deliver_response; an empty answer is a failure (CDR T4)."""

import json
from unittest.mock import AsyncMock
import pytest

from src.domain.agent import AgentStatus, DeliveryItem
from tests.support.agent_answer_fakes import (
    ENVELOPE, FakeEngine, empty_answer_records, result,
)
from src.agents.core import quick_response_agent as MODULE
from tests.unit.agents.core.test_quick_response_agent import (  # noqa: F401  (fixtures)
    quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder, quick_agent,
    create_query_message,
)

RICH = True


@pytest.fixture
def agent(quick_agent):
    return quick_agent


def MESSAGE():
    return create_query_message("hello")


async def _run(agent, monkeypatch, delegation):
    engine = FakeEngine(delegation)
    monkeypatch.setattr(MODULE, "DelegationEngine", engine)
    agent.last_engine = engine
    return await agent.execute(MESSAGE())


def _assert_envelope(response):
    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Here is the answer."
    assert response.metadata["response_summary"] == "Short summary."
    assert response.result.link_list
    if RICH:
        assert response.result.structured_data.content_type == "table"
        assert response.result.structured_data.data["rows"] == [{"cells": ["a", "b"]}]


async def test_passes_deliver_response_as_terminal_tool(agent, monkeypatch):
    await _run(agent, monkeypatch, result(args={"response": json.dumps(ENVELOPE)}))
    assert agent.last_engine.last_kwargs["terminal_tool"] == "deliver_response"


async def test_string_response_envelope_is_parsed(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(args={"response": json.dumps(ENVELOPE)}))
    _assert_envelope(r)


async def test_dict_response_envelope_is_parsed(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(args={"response": ENVELOPE}))
    _assert_envelope(r)


async def test_grok_shape_args_are_parsed(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(args=dict(ENVELOPE)))
    _assert_envelope(r)


async def test_unparseable_string_becomes_the_text(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(args={"response": "just words, no json"}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "just words, no json"


async def test_text_beside_terminal_call_used_when_answer_empty(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(text="Said beside the call.", args={"response": ""}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "Said beside the call."


async def test_empty_text_path_is_a_failure(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(text=""))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(empty_answer_records(caplog)) == 1
    assert empty_answer_records(caplog)[0].via_terminal_tool is False


async def test_empty_terminal_path_is_a_failure(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(args={"response": ""}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(empty_answer_records(caplog)) == 1
    assert empty_answer_records(caplog)[0].via_terminal_tool is True


async def test_whitespace_only_answer_is_a_failure(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(args={"response": "   \n "}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"


async def test_rich_only_answer_is_success(agent, monkeypatch, caplog):
    env = {"full_response": "", "response_summary": "s",
           "rich_content": ENVELOPE["rich_content"], "link_list": []}
    r = await _run(agent, monkeypatch, result(args={"response": json.dumps(env)}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.structured_data.content_type == "table"
    assert not empty_answer_records(caplog)


async def test_delivery_items_only_answer_is_success(agent, monkeypatch, caplog):
    item = DeliveryItem(type="message", data={"text": "extra"})
    r = await _run(agent, monkeypatch, result(args={"response": ""}, delivery_items=[item]))
    assert r.status == AgentStatus.SUCCESS
    assert r.delivery_items == [item]
    assert not empty_answer_records(caplog)


async def test_empty_args_with_text_beside_uses_the_text(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(text="X", args={}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "X"


async def test_empty_args_without_text_is_empty_answer_failure(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(text="", args={}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(empty_answer_records(caplog)) == 1
    assert empty_answer_records(caplog)[0].via_terminal_tool is True


async def test_empty_text_after_successful_async_dispatch_is_success(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(args={"response": ""}, async_dispatches=["create_html_page"]))
    assert r.status == AgentStatus.SUCCESS
    assert not empty_answer_records(caplog)


async def test_empty_text_without_any_dispatch_is_empty_answer(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(args={"response": ""}, async_dispatches=[]))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(empty_answer_records(caplog)) == 1


async def test_bare_tool_code_answer_is_empty_answer(agent, monkeypatch, caplog):
    r = await _run(agent, monkeypatch, result(args={"response": "tool_code"}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(empty_answer_records(caplog)) == 1


async def test_real_engine_ends_on_one_llm_turn_with_deliver_response(quick_agent, mock_llm_port):
    """The looping bug: without the terminal tool the real engine answered 'unknown tool' and went on."""
    from src.ports.llm_port import LLMResponse, ToolCall, UsageMetadata

    call = ToolCall(name="deliver_response", args={"response": json.dumps(ENVELOPE)})
    mock_llm_port.generate_content = AsyncMock(return_value=LLMResponse(
        text="", tool_calls=[call],
        usage_metadata=UsageMetadata(prompt_tokens=5, completion_tokens=5, total_tokens=10),
    ))
    r = await quick_agent.execute(create_query_message("hello"))
    assert mock_llm_port.generate_content.await_count == 1
    _assert_envelope(r)
