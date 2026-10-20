"""Lelik's prompt carries no standing directives (written for Smart; revised 2026-10-21)."""
import pytest

from tests.unit.agents.test_lelik_agent_delegation import _agent


@pytest.mark.asyncio
async def test_session_config_excludes_standing_directives():
    agent, prompt_builder, *_ = _agent()
    await agent.session_config(user_id="u1", account_id="a1")
    kwargs = prompt_builder.build_for_agent.await_args.kwargs
    assert kwargs["include_directives"] is False
    assert kwargs["include_biographical"] is True
