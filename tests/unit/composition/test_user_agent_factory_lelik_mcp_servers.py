"""`UserAgentFactory._build_lelik` hands LelikAgent the voice MCP server descriptors exactly when VOICE_MCP_TOOLS is
on, at the deployment's MCP_RESOURCE_URI (LELIK_VOICE_MCP_RFC §4, S3)."""
from types import SimpleNamespace
from unittest.mock import MagicMock

from src.composition.deploy_topology import build_deploy_topology
from src.composition.user_agent_factory import UserAgentFactory, _UserContext
from src.domain.user import UserBotConfig
from src.domain.voice_mode import VoiceMode


def _build(monkeypatch, knob: str):
    monkeypatch.setenv("VOICE_MCP_TOOLS", knob)
    profile = MagicMock()
    profile.config = UserBotConfig(timezone="Europe/Madrid")
    config = {"TWILIO_PHONE_NUMBER": "+15550101", "CLOUD_RUN_SERVICE_URL": "https://main.example.com",
              "MCP_RESOURCE_URI": "https://mcp.example.test/mcp"}
    fake = SimpleNamespace(
        _telephony=MagicMock(),
        config=config,
        deploy_topology=build_deploy_topology(config, MagicMock()),
        notification_service=MagicMock(), repository=MagicMock(), session_store=MagicMock(),
    )
    return UserAgentFactory._build_lelik(fake, "usr-1", _UserContext(user_profile=profile, prompt_builder=MagicMock()))


def test_the_knob_on_gives_the_agent_the_drive_server(monkeypatch):
    agent = _build(monkeypatch, "on")

    assert [(s.label, s.url) for s in agent._mcp_servers_for(VoiceMode.LELIK)] == [
        ("alek_drive", "https://mcp.example.test/drive-mcp")]


def test_the_knob_off_gives_none(monkeypatch):
    assert _build(monkeypatch, "off")._mcp_servers_for(VoiceMode.LELIK) == ()
