"""An interpreter call has no memory: search_memory is refused there, and a companion call still delegates it."""
import logging

import pytest

from src.domain.voice_mode import VoiceMode
from tests.unit.agents.test_lelik_delegate_intent_allowlist import _agent


async def _search_memory(agent, mode):
    return await agent.delegate_outcome(
        user_id="usr-1", account_id="acc-1", arguments={"intent": "search_memory", "query": "q"}, call_context=[],
        mode=mode)


@pytest.mark.asyncio
async def test_an_interpreter_search_memory_delegation_is_refused(caplog):
    agent = _agent()
    with caplog.at_level(logging.WARNING):
        outcome = await _search_memory(agent, VoiceMode.INTERPRETER)

    assert outcome.failed is True and outcome.text.startswith("Tool error:")
    agent.coordinator.handle_delegation.assert_not_awaited()
    assert "'search_memory' is outside the voice allowlist" in caplog.text


@pytest.mark.asyncio
async def test_a_companion_search_memory_delegation_is_not_refused():
    agent = _agent()

    outcome = await _search_memory(agent, VoiceMode.LELIK)

    assert outcome.failed is False
    agent.coordinator.handle_delegation.assert_awaited_once()


def test_the_interpreter_permits_no_intent_while_the_companion_keeps_its_own():
    # Owner decision 2026-11-11: the interpreter has no tools, so nothing is delegable from it, search_memory included.
    agent = _agent()

    assert agent.permitted_intents(VoiceMode.INTERPRETER) == frozenset()
    assert "search_memory" in agent.permitted_intents(VoiceMode.LELIK)
