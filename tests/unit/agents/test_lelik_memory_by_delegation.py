"""Lelik reaches memory only through the `search_memory` delegation and the drive only through its MCP server
(LELIK_VOICE_MCP_RFC §2, amendment 2026-11-10). The interpreter has no memory at all."""
from unittest.mock import AsyncMock, MagicMock

from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD, voice_session_servers
from src.domain.voice_mode import VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS, Intent
from src.infrastructure.agent_registry import AgentRegistry

SERVERS = voice_session_servers("https://mcp.example.test/mcp")


def _lelik() -> LelikAgent:
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    persona.primary_channel.return_value = None
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES["xai"], mcp_servers=SERVERS,
    )
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    agent.coordinator = AgentCoordinator(registry=registry, task_queue=None, alert_sink=AsyncMock())
    return agent


def _declared(config: dict) -> set:
    delegate = next(tool for tool in config["tools"] if tool["name"] == "delegate_to_specialist")
    return set(delegate["parameters"]["properties"]["intent"].get("enum") or []) | {
        line[2:].split(":", 1)[0] for line in delegate["description"].splitlines() if line.startswith("- ")}


async def test_the_companion_call_on_xai_declares_search_memory_and_carries_only_the_drive_server():
    agent = _lelik()
    config = await agent.session_config(user_id="usr-1", account_id="acc-1", mode=VoiceMode.LELIK)

    assert Intent.SEARCH_MEMORY in _declared(config)
    assert Intent.SEARCH_MEMORY in agent.permitted_intents(VoiceMode.LELIK)
    assert [server["label"] for server in config[VOICE_MCP_SERVERS_FIELD]] == ["alek_drive"]
    assert config[VOICE_MCP_SERVERS_FIELD][0]["allowed_tools"] == ["search_drive", "find_passages", "read_passages"]


async def test_the_interpreter_call_still_has_no_search_memory():
    # Owner decision 2026-11-11: the interpreter has no tools at all, so there is no delegate tool to declare it on.
    agent = _lelik()
    config = await agent.session_config(user_id="usr-1", account_id="acc-1", mode=VoiceMode.INTERPRETER)

    assert config["tools"] == []
    assert VOICE_MCP_SERVERS_FIELD not in config
    assert Intent.SEARCH_MEMORY not in agent.permitted_intents(VoiceMode.INTERPRETER)
