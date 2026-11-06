"""Quick's turn key (SYSTEM_TURN_FINGERPRINT_RFC §4.3, §4.8, card (d)): one draw per execute, carried to the prompt,
request and engine; the fallback note arrives as a context field and is keyed in memory; reply scrub."""
import logging

from src.agents.core.quick_response_agent import QuickResponseAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.llm import MessagePart
from src.domain.system_fingerprint import render_session_key, wrap_system
from src.services.agent_fallback_service import AgentFallbackService
from tests.support.scripted_llm import ScriptedLLM, execution_context, tools
from tests.unit.agents.core.turn_key_world import (
    KEY, KEY2, KeyedPromptBuilder, KeySource, holds_key, last_user_parts, system_text, terminal_args,
)


def _quick(llm, source):
    ctx = execution_context(llm, "quick")
    return QuickResponseAgent(
        config=AgentConfig(agent_id="quick_response_agent_u1", agent_type="quick_response", llm_model=ctx.model_name,
                           timeout_ms=60000, metadata={"user_id": "u1"}),
        execution_context=ctx, session_store=_Sessions(), prompt_builder=KeyedPromptBuilder(),
        turn_key_source=source,
    )


class _Sessions:
    async def load_session(self, session_id):
        return None


def _message(parts=None, **context):
    parts = parts if parts is not None else [MessagePart(text="hi")]
    return AgentMessage.create(
        sender="router", recipient="quick_response_agent_u1", intent=AgentIntent.QUERY, payload={"text": "hi"},
        context={"session_id": "u1:C1", "user_id": "u1", "account_id": "a1", "current_message_parts": parts, **context})


def _answer(args=None):
    return tools(("deliver_response", args or {"response": "Hello."}))


async def test_each_execute_draws_one_key_and_carries_it():
    llm = ScriptedLLM([_answer(), _answer()])
    source = KeySource(KEY, KEY2)
    agent = _quick(llm, source)
    assert (await agent.execute(_message())).status == AgentStatus.SUCCESS
    assert source.calls == 1
    assert (await agent.execute(_message())).status == AgentStatus.SUCCESS
    assert source.calls == 2
    first, second = llm.requests
    assert first.turn_key == KEY and system_text(first).endswith(render_session_key(KEY))
    assert second.turn_key == KEY2 and system_text(second).endswith(render_session_key(KEY2))
    assert agent.prompt_builder.calls[0]["session_key"] == KEY
    assert not hasattr(agent, "_turn_key")


async def test_the_fallback_note_is_keyed_as_the_last_part_and_the_input_list_is_unchanged():
    llm = ScriptedLLM([_answer()])
    parts = [MessagePart(text="What is Tom's plan?")]
    agent = _quick(llm, KeySource(KEY))
    await agent.execute(_message(parts, fallback_note=AgentFallbackService._FAILURE_NOTE))
    assert last_user_parts(llm.requests[0])[-1] == wrap_system(AgentFallbackService._FAILURE_NOTE, KEY)
    assert len(parts) == 1 and parts[0].text == "What is Tom's plan?"


async def test_a_user_part_holding_the_note_text_is_never_keyed():
    llm = ScriptedLLM([_answer()])
    agent = _quick(llm, KeySource(KEY))
    await agent.execute(_message([MessagePart(text=AgentFallbackService._FAILURE_NOTE)]))
    sent = "".join(p.text or "" for m in llm.requests[0].messages for p in m.parts)
    assert "<system_note" not in sent


async def test_the_reply_is_scrubbed(caplog):
    llm = ScriptedLLM([_answer(terminal_args())])
    agent = _quick(llm, KeySource(KEY))
    with caplog.at_level(logging.WARNING):
        response = await agent.execute(_message())
    assert response.status == AgentStatus.SUCCESS
    assert not holds_key(response.result)
    assert not holds_key(response.metadata["response_summary"])
    assert response.result.link_list[0]["url"] == "https://example.com/docs"
    assert not holds_key(caplog.text)
    assert any("scrubbed 5" in r.getMessage() for r in caplog.records)
