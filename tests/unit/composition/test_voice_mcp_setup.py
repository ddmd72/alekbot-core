"""The voice MCP token kind's key, knob and service build (LELIK_VOICE_MCP_RFC §5.2, §11 S1).

`build_application_graph` validates `VOICE_MCP_SIGNING_KEY` before anything else is built (main() swallows the MCP
build's errors): the `VOICE_MCP_TOOLS` knob on without the key refuses the start; the knob off without the key boots as
before. The graph runs over the sealed deploy environment (`tests/support/prod_env.py`).
"""
from typing import Any

import pytest

from src.composition import voice_mcp_setup
from src.composition.application_graph import build_application_graph
from src.config import settings
from src.domain.mcp_resource import MCPResource
from src.services.voice_mcp_token_service import VoiceMcpTokenService
from tests.support import prod_env
from tests.support.voice_mcp_kit import AUTH_CONFIG, DIGEST_KEY, OAUTH_SECRET, VOICE_KEY, Kit

# An OAuth secret long enough to be a valid voice key (48+), so the equality check is what refuses it, not the length.
LONG_OAUTH_SECRET = "oauth-session-secret-" + "0123456789abcdef" * 2 + "-test"


class UntouchableDb:
    """A Firestore client the graph must not reach: any attribute access fails the test."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"build_application_graph touched the database ({name}) before refusing to start")


class TestSigningKey:
    def test_absent_with_the_knob_off_is_none(self):
        assert voice_mcp_setup.signing_key(None, oauth_secret=OAUTH_SECRET, tools_enabled=False) is None
        assert voice_mcp_setup.signing_key("  ", oauth_secret=OAUTH_SECRET, tools_enabled=False) is None

    @pytest.mark.parametrize("raw", [None, "", " \n"])
    def test_absent_with_the_knob_on_refuses(self, raw):
        with pytest.raises(ValueError, match="VOICE_MCP_TOOLS is on but VOICE_MCP_SIGNING_KEY is not set"):
            voice_mcp_setup.signing_key(raw, oauth_secret=OAUTH_SECRET, tools_enabled=True)

    @pytest.mark.parametrize("tools_enabled", [False, True])
    def test_a_short_key_refuses(self, tools_enabled):
        with pytest.raises(ValueError, match="at least 48 characters"):
            voice_mcp_setup.signing_key("k" * 47, oauth_secret=OAUTH_SECRET, tools_enabled=tools_enabled)

    def test_the_oauth_secret_is_never_the_voice_key(self):
        assert len(LONG_OAUTH_SECRET) >= 48
        with pytest.raises(ValueError, match="must differ from OAUTH_SESSION_SECRET"):
            voice_mcp_setup.signing_key(LONG_OAUTH_SECRET + "\n", oauth_secret=LONG_OAUTH_SECRET, tools_enabled=True)

    def test_a_valid_key_is_stripped(self):
        assert voice_mcp_setup.signing_key(f" {VOICE_KEY}\n", oauth_secret=None, tools_enabled=True) == VOICE_KEY


class TestKnob:
    @pytest.mark.parametrize("value, on", [("on", True), (" ON ", True), ("off", False), ("true", False), ("", False)])
    def test_only_on_is_on(self, monkeypatch, value, on):
        monkeypatch.setenv("VOICE_MCP_TOOLS", value)
        assert voice_mcp_setup.voice_mcp_tools_enabled() is on

    def test_unset_is_off(self, monkeypatch):
        monkeypatch.delenv("VOICE_MCP_TOOLS", raising=False)
        assert voice_mcp_setup.voice_mcp_tools_enabled() is False


class TestBuild:
    def test_no_key_no_service(self):
        kit = Kit()
        assert voice_mcp_setup.build_voice_mcp_tokens(
            key=None, store=kit.store, digest_key=DIGEST_KEY, auth_config=AUTH_CONFIG, user_repo=kit.user_repo,
            whitelist_repo=kit.whitelist_repo, alert_sink=None) is None

    async def test_a_key_builds_the_service_over_the_drive_resource_uri(self):
        kit = Kit()
        service = voice_mcp_setup.build_voice_mcp_tokens(
            key=VOICE_KEY, store=kit.store, digest_key=DIGEST_KEY, auth_config=AUTH_CONFIG, user_repo=kit.user_repo,
            whitelist_repo=kit.whitelist_repo, alert_sink=kit.alerts)
        assert isinstance(service, VoiceMcpTokenService)
        tokens = await service.mint("ticket-0003-invented", "usr-1", "acc-1")
        # same key, same URIs, same scopes as the kit's own service: each verifies the other's tokens
        assert await kit.voice.verify(tokens.drive, MCPResource.DRIVE) is not None


class TestGraph:
    def test_the_knob_on_without_the_key_refuses_the_start(self, monkeypatch):
        config, _ = prod_env.prod_settings(monkeypatch)
        config["VOICE_MCP_SIGNING_KEY"] = None
        monkeypatch.setenv("VOICE_MCP_TOOLS", "on")
        with pytest.raises(ValueError, match="VOICE_MCP_TOOLS is on but VOICE_MCP_SIGNING_KEY is not set"):
            build_application_graph(config, UntouchableDb())

    def test_a_key_equal_to_the_oauth_secret_refuses_the_start(self, monkeypatch):
        config, _ = prod_env.prod_settings(monkeypatch)
        config["OAUTH_SESSION_SECRET"] = LONG_OAUTH_SECRET
        config["VOICE_MCP_SIGNING_KEY"] = config["OAUTH_SESSION_SECRET"]
        with pytest.raises(ValueError, match="must differ from OAUTH_SESSION_SECRET"):
            build_application_graph(config, UntouchableDb())

    @pytest.mark.slow
    def test_the_knob_off_without_the_key_builds_with_no_voice_key(self, monkeypatch):
        config, _ = prod_env.prod_settings(monkeypatch)
        config["VOICE_MCP_SIGNING_KEY"] = None
        services = prod_env.install_offline_services(monkeypatch)
        # after the deploy env is installed: the deploy sets the knob on, so it is turned off here, not before
        monkeypatch.delenv("VOICE_MCP_TOOLS", raising=False)
        assert voice_mcp_setup.voice_mcp_tools_enabled() is False
        graph = build_application_graph(config, services.db)
        assert graph.voice_mcp_signing_key is None

    @pytest.mark.slow
    def test_the_deployed_key_reaches_the_graph_and_never_its_repr(self, monkeypatch):
        config, fallbacks = prod_env.prod_settings(monkeypatch)
        services = prod_env.install_offline_services(monkeypatch)
        graph = build_application_graph(config, services.db)
        assert graph.voice_mcp_signing_key == prod_env.SECRET_VALUES["VOICE_MCP_SIGNING_KEY"]
        assert graph.voice_mcp_signing_key not in repr(graph)
        assert "VOICE_MCP_SIGNING_KEY" not in fallbacks


def test_load_settings_declares_the_key(monkeypatch):
    config, fallbacks = prod_env.prod_settings(monkeypatch)
    assert config["VOICE_MCP_SIGNING_KEY"] == prod_env.SECRET_VALUES["VOICE_MCP_SIGNING_KEY"]
    assert fallbacks == []


def test_load_settings_survives_a_secret_manager_miss(monkeypatch):
    """Not mounted and absent from Secret Manager: `get_secret` returns None and the key is None, not an error."""
    real = prod_env.secret_names()
    monkeypatch.setattr(prod_env, "secret_names", lambda: [n for n in real if n != "VOICE_MCP_SIGNING_KEY"])
    config, fallbacks = prod_env.prod_settings(monkeypatch)
    assert fallbacks == ["VOICE_MCP_SIGNING_KEY"] and config["VOICE_MCP_SIGNING_KEY"] is None


def test_a_secret_missing_in_secret_manager_is_none_not_an_error(monkeypatch):
    """The boot path's real `get_secret` over a client that answers NotFound: None, so a key absent from Secret
    Manager leaves the setting empty and the start goes on (with the knob off)."""
    from google.api_core.exceptions import NotFound

    class Client:
        def access_secret_version(self, request):
            raise NotFound("secret not found")

    monkeypatch.setattr(settings.secretmanager, "SecretManagerServiceClient", Client)
    assert settings.get_secret("p-test", "VOICE_MCP_SIGNING_KEY") is None
