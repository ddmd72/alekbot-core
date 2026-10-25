"""Tutor: deliver shapes parse; an empty answer is a failure (CDR T4)."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentStatus
from tests.support.agent_answer_fakes import (
    ENVELOPE, FakeEngine, empty_answer_records, result,
)
from src.adapters.in_memory_provider_resilience import InMemoryProviderResilience
from src.agents import tutor_agent as MODULE
from src.agents.tutor_agent import TutorAgent
from src.domain.agent import AgentConfig
from src.domain.user import PerformanceTier
from src.ports.llm_port import AgentExecutionContext, LLMPort, ProviderCapabilities
from tests.unit.agents.test_tutor_agent import (  # noqa: F401  (fixtures)
    mock_prompt_builder, mock_assembler, _bound_message,
)

RICH = False


def _make_tutor(mock_prompt_builder, mock_assembler, llm_text="", with_tools=True):
    llm = AsyncMock(spec=LLMPort)
    llm.generate_content.return_value = MagicMock(
        text=llm_text, usage_metadata=MagicMock(total_tokens=3), tool_calls=[],
    )
    config = AgentConfig(agent_id="tutor_agent_test", agent_type="tutor",
                         timeout_ms=60000, capabilities=["tutor_chat"])
    ctx = AgentExecutionContext(
        agent_type="tutor", provider=llm, model_name="claude-sonnet-5", tier=PerformanceTier.BALANCED,
        capabilities=ProviderCapabilities(), provider_name="claude",
        resilience_port=InMemoryProviderResilience(),
    )
    tutor = TutorAgent(config=config, execution_context=ctx, prompt_builder=mock_prompt_builder,
                       assembler=mock_assembler, user_id=None)
    if with_tools:
        tutor.coordinator = MagicMock()
        tutor.coordinator.get_available_intents_for.return_value = [
            {"name": "search_web", "description": "Search.", "context_schema": {}},
        ]
    return tutor


@pytest.fixture
def agent(mock_prompt_builder, mock_assembler):
    return _make_tutor(mock_prompt_builder, mock_assembler)


def MESSAGE():
    return _bound_message()


async def _run(agent, monkeypatch, delegation):
    engine = FakeEngine(delegation)
    monkeypatch.setattr(MODULE, "DelegationEngine", engine)
    agent.last_engine = engine
    return await agent.execute(MESSAGE())


def _assert_envelope(response):
    assert response.status == AgentStatus.SUCCESS
    assert response.result.text == "Here is the answer."
    assert response.metadata["response_summary"] == "Short summary."
    if RICH:
        assert response.result.structured_data.content_type == "table"
        assert response.result.structured_data.data["rows"] == [{"cells": ["a", "b"]}]


async def test_passes_deliver_response_as_terminal_tool(agent, monkeypatch):
    await _run(agent, monkeypatch, result(args={"response": json.dumps(ENVELOPE)}))
    assert agent.last_engine.last_kwargs["terminal_tool"] == "deliver_response"


async def test_no_tools_path_empty_text_is_a_failure(mock_prompt_builder, mock_assembler, caplog):
    tutor = _make_tutor(mock_prompt_builder, mock_assembler, llm_text="", with_tools=False)
    r = await tutor.execute(MESSAGE())
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert empty_answer_records(caplog)[0].via_terminal_tool is False


async def test_no_tools_path_plain_text_is_success(mock_prompt_builder, mock_assembler):
    tutor = _make_tutor(mock_prompt_builder, mock_assembler, llm_text="¡Hola!", with_tools=False)
    r = await tutor.execute(MESSAGE())
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "¡Hola!"


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
