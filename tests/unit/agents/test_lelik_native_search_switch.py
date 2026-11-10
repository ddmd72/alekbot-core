"""The LELIK_XAI_NATIVE_SEARCH switch (owner decision 2026-11-08): on an xAI call, switched on, the session offers
xAI's own web search and the delegate tool loses search_web_light; switched off, or on OpenAI, nothing changes.

Built over the real registry of ALL_DESCRIPTORS and the real coordinator, as test_lelik_send_to_chat_tool.py.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.lelik_agent import LelikAgent
from src.composition.user_agent_factory import _lelik_xai_native_search
from src.domain.lelik_context import LelikContext
from src.domain.voice_mode import VoiceMode
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.domain.voice_session_spec import VoiceSessionSpec
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry


def _lelik(provider: str, native_web_search: bool) -> LelikAgent:
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "PROMPT"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES[provider], native_web_search=native_web_search,
    )
    agent.coordinator = AgentCoordinator(registry=registry, task_queue=None, alert_sink=AsyncMock())
    return agent


async def _config(agent: LelikAgent, mode: VoiceMode = VoiceMode.LELIK) -> dict:
    return await agent.session_config(user_id="u1", account_id="a1", mode=mode)


def _delegate_description(config: dict) -> str:
    return next(t for t in config["tools"] if t["name"] == "delegate_to_specialist")["description"]


class TestSwitchedOnXai:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", list(VoiceMode))
    async def test_the_delegate_tool_offers_no_light_web_search_in_any_mode(self, mode):
        config = await _config(_lelik("xai", native_web_search=True), mode)

        description = _delegate_description(config)
        assert "- search_web_light:" not in description
        assert "- ask_alek:" in description
        # An interpreter call has no memory search (owner rule 2026-11-10); every other mode keeps it.
        assert ("- search_memory:" in description) is (mode is not VoiceMode.INTERPRETER)
        assert "send_to_chat" in {t["name"] for t in config["tools"]}

    @pytest.mark.asyncio
    async def test_the_session_spec_asks_the_relay_for_built_in_search(self):
        config = await _config(_lelik("xai", native_web_search=True))

        spec = VoiceSessionSpec.from_dict(config["voice"])
        assert spec.builtin_search is True
        assert spec.provider == "xai"


class TestSwitchedOffOrNotXai:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,native", [("xai", False), ("openai", False), ("openai", True)])
    async def test_the_delegate_tool_keeps_the_light_web_search(self, provider, native):
        config = await _config(_lelik(provider, native_web_search=native))

        assert "- search_web_light:" in _delegate_description(config)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider,native", [("xai", False), ("openai", False), ("openai", True)])
    async def test_the_session_spec_is_the_profiles_unchanged(self, provider, native):
        config = await _config(_lelik(provider, native_web_search=native))

        assert config["voice"] == VOICE_PROVIDER_PROFILES[provider].session.to_dict()
        assert "builtin_search" not in config["voice"]

    @pytest.mark.asyncio
    async def test_the_switch_is_off_by_default(self):
        agent = LelikAgent(
            config=MagicMock(agent_id="lelik_agent_u1"), telephony=AsyncMock(), from_number="+1",
            status_callback_url="https://x/voice/status", prompt_builder=AsyncMock(), persona=AsyncMock(),
            notifications=AsyncMock(), voice_profile=VOICE_PROVIDER_PROFILES["xai"],
        )
        assert agent._native_web_search is False


class TestTheKnob:
    @pytest.mark.parametrize("value,expected", [
        (None, False), ("", False), ("off", False), ("true", False), ("1", False), ("on", True), (" ON ", True),
    ])
    def test_only_on_switches_it_on(self, monkeypatch, value, expected):
        if value is None:
            monkeypatch.delenv("LELIK_XAI_NATIVE_SEARCH", raising=False)
        else:
            monkeypatch.setenv("LELIK_XAI_NATIVE_SEARCH", value)

        assert _lelik_xai_native_search() is expected


def test_only_the_xai_profile_has_a_built_in_web_search():
    assert {name for name, p in VOICE_PROVIDER_PROFILES.items() if p.builtin_web_search} == {"xai"}
