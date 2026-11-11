"""An interpreter call has no tools at all (owner decision 2026-11-11): it only translates. Its session is built with an
empty tool list, no MCP server, no token, and nothing it could name is delegable. The companion call keeps its tools."""
import pytest

from src.domain.voice_mcp_token import voice_mcp_eligible
from src.domain.voice_mode import VoiceMode
from tests.unit.agents.test_lelik_session_config_mcp_servers import _config, _lelik
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD


async def test_an_interpreter_session_is_built_with_zero_tools():
    config = await _config(_lelik(), VoiceMode.INTERPRETER)

    assert config["tools"] == []
    assert VOICE_MCP_SERVERS_FIELD not in config


async def test_an_interpreter_call_delegates_nothing():
    assert _lelik().permitted_intents(VoiceMode.INTERPRETER) == frozenset()


@pytest.mark.parametrize("provider", ["xai", "openai"])
def test_no_mcp_token_is_minted_for_an_interpreter_call(provider):
    assert voice_mcp_eligible(VoiceMode.INTERPRETER.value, provider) is False


async def test_the_companion_mode_keeps_its_tools():
    agent = _lelik()
    config = await _config(agent, VoiceMode.LELIK)

    assert {tool["name"] for tool in config["tools"]} == {"delegate_to_specialist", "send_to_chat"}
    assert agent.permitted_intents(VoiceMode.LELIK) != frozenset()
