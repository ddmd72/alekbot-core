"""Quick ends on deliver_response; an empty answer is a failure (CDR T4)."""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from src.domain.agent import AgentStatus, DeliveryItem
from src.infrastructure.delegation_engine import DelegationResult

ENVELOPE = {
    "full_response": "Here is the answer.",
    "response_summary": "Short summary.",
    "rich_content": {
        "type": "table",
        "data": {"rows": [{"cells": ["a", "b"]}]},
        "fallback": "a | b",
    },
    "link_list": [{"anchor": "1", "title": "Docs", "url": "https://example.com/docs"}],
}


class FakeEngine:
    """Stands in for DelegationEngine: records execute() kwargs, returns a canned result."""

    last_kwargs: dict = {}

    def __init__(self, result):
        self._result = result

    def __call__(self, coordinator):
        return self

    async def execute(self, **kwargs):
        FakeEngine.last_kwargs = kwargs
        return self._result


class ErrorLog:
    """Captures logger.error calls (message + extra) of the agent module under test."""

    def __init__(self):
        self.records = []

    def __call__(self, msg, *args, **kwargs):
        self.records.append((msg % args if args else msg, kwargs.get("extra") or {}))

    @property
    def empty_answers(self):
        return [r for r in self.records if r[1].get("event") == "empty_answer"]


def result(text="", args=None, **kw):
    return DelegationResult(text=text, total_tokens=7, terminal_tool_args=args, **kw)

from src.agents.core import quick_response_agent as MODULE
from tests.unit.agents.core.test_quick_response_agent import (  # noqa: F401  fixtures + helper
    quick_agent_config, mock_llm_port, mock_session_store, mock_prompt_builder, quick_agent,
    create_query_message,
)

RICH = True


@pytest.fixture
def agent(quick_agent):
    return quick_agent


def MESSAGE():
    return create_query_message("hello")


@pytest.fixture
def errors(monkeypatch):
    log = ErrorLog()
    monkeypatch.setattr(MODULE.logger, "error", log)
    return log


async def _run(agent, monkeypatch, delegation):
    monkeypatch.setattr(MODULE, "DelegationEngine", FakeEngine(delegation))
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
    assert FakeEngine.last_kwargs["terminal_tool"] == "deliver_response"


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


async def test_empty_text_path_is_a_failure(agent, monkeypatch, errors):
    r = await _run(agent, monkeypatch, result(text=""))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(errors.empty_answers) == 1
    assert errors.empty_answers[0][1]["via_terminal_tool"] is False


async def test_empty_terminal_path_is_a_failure(agent, monkeypatch, errors):
    r = await _run(agent, monkeypatch, result(args={"response": ""}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(errors.empty_answers) == 1
    assert errors.empty_answers[0][1]["via_terminal_tool"] is True


async def test_whitespace_only_answer_is_a_failure(agent, monkeypatch, errors):
    r = await _run(agent, monkeypatch, result(args={"response": "   \n "}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"


async def test_rich_only_answer_is_success(agent, monkeypatch, errors):
    env = {"full_response": "", "response_summary": "s",
           "rich_content": ENVELOPE["rich_content"], "link_list": []}
    r = await _run(agent, monkeypatch, result(args={"response": json.dumps(env)}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.structured_data.content_type == "table"
    assert not errors.empty_answers


async def test_delivery_items_only_answer_is_success(agent, monkeypatch, errors):
    item = DeliveryItem(type="message", data={"text": "extra"})
    r = await _run(agent, monkeypatch, result(args={"response": ""}, delivery_items=[item]))
    assert r.status == AgentStatus.SUCCESS
    assert r.delivery_items == [item]
    assert not errors.empty_answers


async def test_empty_args_with_text_beside_uses_the_text(agent, monkeypatch):
    r = await _run(agent, monkeypatch, result(text="X", args={}))
    assert r.status == AgentStatus.SUCCESS
    assert r.result.text == "X"


async def test_empty_args_without_text_is_empty_answer_failure(agent, monkeypatch, errors):
    r = await _run(agent, monkeypatch, result(text="", args={}))
    assert r.status == AgentStatus.FAILED
    assert r.error == "empty_answer"
    assert len(errors.empty_answers) == 1
