"""NotesAgent must forward its own message context when it delegates.

`_run` received only user_id/account_id, so the delegation it makes rebuilt a context
from those two fields and discarded everything else. That silently reset the delegation
call chain — a cycle passing through notes would have been invisible to the guard — and
it also dropped session_id / origin_channel_id, which async delivery relies on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.domain.agent import AgentMessage, AgentIntent, AgentResponse
from src.domain.llm import LLMResponse, ToolCall


def _agent():
    from src.agents.notes_agent import NotesAgent
    from src.domain.agent import AgentConfig
    from src.ports.agent_note_port import AgentNotePort
    from src.ports.llm_port import AgentExecutionContext
    from src.ports.prompt_builder_port import PromptBuilderPort
    from src.ports.recurrence_port import RecurrencePort

    execution_context = MagicMock(spec=AgentExecutionContext)
    execution_context.provider = AsyncMock()
    execution_context.model_name = "test-model"

    prompt_builder = AsyncMock(spec=PromptBuilderPort)
    prompt_builder.build_for_agent.return_value = "sys"

    agent = NotesAgent(
        config=AgentConfig(
            agent_id="notes_agent_u1",
            agent_type="notes",
            timeout_ms=10_000,
            capabilities=["note_management"],
        ),
        execution_context=execution_context,
        notes_port=AsyncMock(spec=AgentNotePort),
        recurrence=MagicMock(spec=RecurrencePort),
        prompt_builder=prompt_builder,
    )
    agent.coordinator = MagicMock()
    agent.coordinator.handle_delegation = AsyncMock(
        return_value=AgentResponse.success(task_id="t", agent_id="x", result="done")
    )
    agent.coordinator.get_available_intents_for = MagicMock(return_value=[])
    agent._on_delegation = MagicMock()
    return agent


def _message(context: dict) -> AgentMessage:
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"query": "remind me tomorrow", "intent": "manage_self_reminders"}
    msg.context = context
    return msg


CONTEXT = {
    "user_id": "u1",
    "account_id": "acc1",
    "session_id": "u1:C123",
    "origin_channel_id": "C123",
    "_call_chain": ["smart_agent", "notes_agent"],
}


class TestNotesAgentForwardsContext:

    async def test_call_chain_is_forwarded(self):
        """Without this the guard cannot see a cycle that passes through notes."""
        agent = _agent()
        agent._run = AsyncMock(return_value={"summary": "ok"})

        await agent.execute(_message(dict(CONTEXT)))

        forwarded = agent._run.await_args.args[-1]
        assert forwarded["_call_chain"] == ["smart_agent", "notes_agent"]

    async def test_full_context_is_forwarded_not_just_ids(self):
        agent = _agent()
        agent._run = AsyncMock(return_value={"summary": "ok"})

        await agent.execute(_message(dict(CONTEXT)))

        forwarded = agent._run.await_args.args[-1]
        assert forwarded["session_id"] == "u1:C123"
        assert forwarded["origin_channel_id"] == "C123"

    async def test_delegation_receives_the_context(self):
        """End of the thread: what _run hands to handle_delegation."""
        agent = _agent()
        agent._build_system_prompt = AsyncMock(return_value="sys")
        agent._build_delegate_tool_declaration = MagicMock(return_value={"name": "d"})
        agent._call_llm = AsyncMock(return_value=LLMResponse(
            text="",
            tool_calls=[ToolCall(name="delegate_to_specialist",
                                 args={"intent": "compute_math", "query": "2+2"})],
        ))

        await agent._run("q", "u1", "acc1", dict(CONTEXT))

        passed = agent.coordinator.handle_delegation.await_args.kwargs["context"]
        assert passed["_call_chain"] == ["smart_agent", "notes_agent"]
        assert passed["user_id"] == "u1"
