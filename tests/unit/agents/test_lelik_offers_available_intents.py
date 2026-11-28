"""Lelik's session declares only the intents available on the caller's profile (CONFIG_OVERRIDE_HIERARCHY_RFC §3).
The declaration only shapes what the model is told: `permitted_intents` (what /voice/delegate enforces) and the
`handle_delegation` gate are untouched. Alex Doe is `usr-1`, Clara is `usr-2`."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext
from src.domain.voice_mode import VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, LELIK, Intent
from src.infrastructure.agent_registry import system_intent_defaults
from tests.support.denied_profile import coordinator_denying, delegate_intents, delegate_tool


def _lelik(coordinator, provider="openai", native_web_search=False) -> LelikAgent:
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES[provider], native_web_search=native_web_search,
    )
    agent.coordinator = coordinator
    return agent


async def _offered(agent, user_id="usr-1", mode=VoiceMode.LELIK) -> list:
    config = await agent.session_config(user_id=user_id, account_id="acc-1", mode=mode)
    return delegate_intents(delegate_tool(config["tools"]))


async def test_an_intent_denied_to_the_caller_is_not_offered_and_the_rest_is():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    full = await _offered(_lelik(coordinator), "usr-2")
    assert Intent.SEARCH_MEMORY in full

    assert await _offered(_lelik(coordinator)) == [name for name in full if name != Intent.SEARCH_MEMORY]


async def test_a_caller_with_no_denies_gets_the_declaration_built_from_the_registry():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})
    expected = LelikAgent._build_delegate_tool_declaration(coordinator.get_available_intents_for(LELIK))

    config = await _lelik(coordinator).session_config(user_id="usr-2", account_id="acc-1")

    assert delegate_tool(config["tools"]) == expected


async def test_the_native_search_removal_applies_on_top_of_the_filter():
    coordinator = coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}})

    offered = await _offered(_lelik(coordinator, "xai", native_web_search=True))

    assert Intent.SEARCH_MEMORY not in offered and Intent.SEARCH_WEB_LIGHT not in offered
    assert Intent.ASK_ALEK in offered


async def test_the_enforcement_allowlist_does_not_follow_the_user():
    agent = _lelik(coordinator_denying({"usr-1": {Intent.SEARCH_MEMORY}}))
    await _offered(agent)

    assert Intent.SEARCH_MEMORY in agent.permitted_intents(VoiceMode.LELIK)


async def test_every_gated_intent_denied_leaves_only_the_ungated_ones():
    # `ask_alek` and `send_to_chat` belong to a declaration that is never access-gated (AgentDescriptor.access_gated).
    names = await _offered(_lelik(coordinator_denying({})), "usr-2")
    gated = system_intent_defaults(ALL_DESCRIPTORS)

    offered = await _offered(_lelik(coordinator_denying({"usr-1": names})))

    assert offered == [name for name in names if name not in gated] == [Intent.ASK_ALEK]
