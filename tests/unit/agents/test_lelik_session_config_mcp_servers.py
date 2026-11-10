"""Lelik's session config carries the memory and drive MCP servers only on the plain companion call on xAI with the
VOICE_MCP_TOOLS knob on, and such a call has no search_memory delegation, neither declared nor permitted
(LELIK_VOICE_MCP_RFC §4, §8 O4, O6, O7; §11 S3). An interpreter call has none either, in any configuration: it has no
memory at all (owner rule 2026-11-10).

The knob reaches LelikAgent as its `mcp_servers` (composition: `voice_mcp_servers`): given = knob on, empty = off.
Built over the real registry of ALL_DESCRIPTORS and the real coordinator.
"""
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.domain.agent import AgentResponse
from src.domain.lelik_context import LelikContext
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD, voice_session_servers
from src.domain.voice_mode import VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry

FACTS_URI = "https://mcp.example.test/mcp"
SERVERS = voice_session_servers(FACTS_URI)


def _lelik(provider: str = "xai", mcp_servers=SERVERS) -> LelikAgent:
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES[provider], mcp_servers=mcp_servers,
    )
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    agent.coordinator = AgentCoordinator(registry=registry, task_queue=None, alert_sink=AsyncMock())
    return agent


async def _config(agent: LelikAgent, mode: VoiceMode = VoiceMode.LELIK) -> dict:
    return await agent.session_config(user_id="usr-1", account_id="acc-1", mode=mode)


def _declared_intents(config: dict) -> set:
    delegate = next(tool for tool in config["tools"] if tool["name"] == "delegate_to_specialist")
    return set(delegate["parameters"]["properties"]["intent"].get("enum") or []) | {
        line[2:].split(":", 1)[0] for line in delegate["description"].splitlines() if line.startswith("- ")}


class TestServersOnlyForTheEligibleCall:
    async def test_the_companion_call_on_xai_with_the_knob_on_carries_both_servers(self):
        config = await _config(_lelik())

        assert config[VOICE_MCP_SERVERS_FIELD] == [
            {"label": "alek_memory", "url": "https://mcp.example.test/mcp",
             "description": SERVERS[0].description, "allowed_tools": ["get_user_context"]},
            {"label": "alek_drive", "url": "https://mcp.example.test/drive-mcp",
             "description": SERVERS[1].description, "allowed_tools": ["search_drive", "find_passages", "read_passages"]},
        ]

    @pytest.mark.parametrize("mode", [VoiceMode.INTERPRETER, VoiceMode.TUTOR])
    async def test_interpreter_and_tutor_calls_carry_none(self, mode):
        assert VOICE_MCP_SERVERS_FIELD not in await _config(_lelik(), mode)

    async def test_an_openai_call_carries_none(self):
        assert VOICE_MCP_SERVERS_FIELD not in await _config(_lelik(provider="openai"))

    async def test_with_the_knob_off_no_call_carries_any(self):
        assert VOICE_MCP_SERVERS_FIELD not in await _config(_lelik(mcp_servers=()))

    async def test_the_session_config_holds_no_token(self):
        servers = tuple(server.with_token("eyJh.secret-token.sig") for server in SERVERS)
        config = await _config(_lelik(mcp_servers=servers))

        assert "secret-token" not in json.dumps(config)
        assert all("token" not in d and "authorization" not in d for d in config[VOICE_MCP_SERVERS_FIELD])


class TestSearchMemoryLeavesExactlyThen:
    async def test_the_mcp_call_declares_no_search_memory_and_permits_none(self):
        agent = _lelik()
        config = await _config(agent)

        assert Intent.SEARCH_MEMORY not in _declared_intents(config)
        assert Intent.ASK_ALEK in _declared_intents(config)
        assert Intent.SEARCH_MEMORY not in agent.permitted_intents(VoiceMode.LELIK)

    @pytest.mark.parametrize("provider,mcp_servers,mode", [
        ("xai", SERVERS, VoiceMode.TUTOR),
        ("openai", SERVERS, VoiceMode.LELIK),
        ("xai", (), VoiceMode.LELIK),
    ])
    async def test_every_other_call_declares_and_permits_search_memory(self, provider, mcp_servers, mode):
        agent = _lelik(provider=provider, mcp_servers=mcp_servers)
        config = await _config(agent, mode)

        assert VOICE_MCP_SERVERS_FIELD not in config
        assert Intent.SEARCH_MEMORY in _declared_intents(config)
        assert Intent.SEARCH_MEMORY in agent.permitted_intents(mode)

    @pytest.mark.parametrize("mcp_servers", [SERVERS, ()])
    async def test_an_interpreter_call_declares_and_permits_no_search_memory(self, mcp_servers):
        """Owner rule 2026-11-10: an interpreter call (xAI only) has no memory search, with or without the MCP servers,
        and still carries no server of its own."""
        agent = _lelik(mcp_servers=mcp_servers)
        config = await _config(agent, VoiceMode.INTERPRETER)

        assert VOICE_MCP_SERVERS_FIELD not in config
        assert Intent.SEARCH_MEMORY not in _declared_intents(config)
        assert Intent.ASK_ALEK in _declared_intents(config)
        assert Intent.SEARCH_MEMORY not in agent.permitted_intents(VoiceMode.INTERPRETER)

    async def test_a_model_naming_search_memory_on_the_mcp_call_is_refused_before_dispatch(self):
        agent = _lelik()
        agent.coordinator.handle_delegation = AsyncMock(
            return_value=AgentResponse.success(task_id="t", agent_id="a", result="ok"))

        outcome = await agent.delegate_outcome("usr-1", "acc-1", {"intent": "search_memory", "query": "q"},
                                               call_context=[], mode=VoiceMode.LELIK)

        assert outcome.failed is True and outcome.text.startswith("Tool error:")
        agent.coordinator.handle_delegation.assert_not_awaited()

    async def test_the_same_intent_on_a_tutor_call_still_dispatches(self):
        agent = _lelik()
        agent.coordinator.handle_delegation = AsyncMock(
            return_value=AgentResponse.success(task_id="t", agent_id="a", result="ok"))

        outcome = await agent.delegate_outcome("usr-1", "acc-1", {"intent": "search_memory", "query": "q"},
                                               call_context=[], mode=VoiceMode.TUTOR)

        assert outcome.failed is False and outcome.text == "ok"

    async def test_the_same_intent_on_an_interpreter_call_is_refused_before_dispatch(self):
        agent = _lelik()
        agent.coordinator.handle_delegation = AsyncMock(
            return_value=AgentResponse.success(task_id="t", agent_id="a", result="ok"))

        outcome = await agent.delegate_outcome("usr-1", "acc-1", {"intent": "search_memory", "query": "q"},
                                               call_context=[], mode=VoiceMode.INTERPRETER)

        assert outcome.failed is True and outcome.text.startswith("Tool error:")
        agent.coordinator.handle_delegation.assert_not_awaited()
