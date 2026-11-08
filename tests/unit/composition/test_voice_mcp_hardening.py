"""Security-review hardening at the composition seams (LELIK_VOICE_MCP_RFC §5.2-§5.3).

- A voice-key token that claims an extra drive tool is refused that tool over HTTP, with no read.
- The signing key is checked against the OAuth secret actually in effect (AuthConfig's, the dev placeholder when the
  setting is unset), the public dev placeholder is always refused, and the minimum is 48 characters.
"""
from typing import Any

import pytest

from src.composition import voice_mcp_setup
from src.composition.application_graph import build_application_graph
from src.config.auth import _DEV_DEFAULT_SESSION_SECRET
from tests.support import prod_env
from tests.support.voice_mcp_kit import TICKET, VOICE_KEY, Kit, resign

from .drive_mcp_access_world import NEUTRAL, World
from .test_voice_mcp_resources import _drive_call

LONG_OAUTH_SECRET = "o" * 64


class UntouchableDb:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"build_application_graph touched the database ({name}) before refusing to start")


async def test_a_claimed_extra_drive_tool_is_refused_with_no_read():
    w = await World().build(connector=None)
    kit = Kit(users=("u",))
    tokens = await kit.voice.mint(TICKET, "u", "acc-1")
    widened = resign(tokens.drive, VOICE_KEY, tools=["search_drive", "find_passages", "read_passages",
                                                     "open_drive_file"])
    text = await _drive_call(w, kit, widened, "open_drive_file", file_ref=w.ref(w.plan))
    assert text.split("\n") == NEUTRAL
    assert w.conversion.opened == []


class TestKeyCheck:
    def test_a_47_character_key_is_refused_and_48_accepted(self):
        with pytest.raises(ValueError, match="at least 48 characters"):
            voice_mcp_setup.signing_key("k" * 47, oauth_secret=LONG_OAUTH_SECRET, tools_enabled=False)
        assert voice_mcp_setup.signing_key("k" * 48, oauth_secret=LONG_OAUTH_SECRET, tools_enabled=False) == "k" * 48

    @pytest.mark.parametrize("oauth_secret", [None, LONG_OAUTH_SECRET])
    def test_the_dev_placeholder_is_always_refused(self, oauth_secret):
        assert len(_DEV_DEFAULT_SESSION_SECRET) >= 48
        with pytest.raises(ValueError, match="public dev placeholder"):
            voice_mcp_setup.signing_key(_DEV_DEFAULT_SESSION_SECRET, oauth_secret=oauth_secret, tools_enabled=True)

    def test_a_key_equal_to_the_oauth_secret_in_effect_is_refused(self):
        with pytest.raises(ValueError, match="must differ from OAUTH_SESSION_SECRET"):
            voice_mcp_setup.signing_key(LONG_OAUTH_SECRET, oauth_secret=LONG_OAUTH_SECRET, tools_enabled=False)

    def test_the_secret_in_effect_is_authconfigs(self, monkeypatch):
        monkeypatch.delenv("OAUTH_SESSION_SECRET", raising=False)
        assert voice_mcp_setup.effective_oauth_secret({}) == _DEV_DEFAULT_SESSION_SECRET
        assert voice_mcp_setup.effective_oauth_secret({"OAUTH_SESSION_SECRET": LONG_OAUTH_SECRET}) == LONG_OAUTH_SECRET

    def test_the_graph_refuses_a_key_equal_to_the_oauth_secret_in_effect(self, monkeypatch):
        config, _ = prod_env.prod_settings(monkeypatch)
        config["OAUTH_SESSION_SECRET"] = LONG_OAUTH_SECRET
        config["VOICE_MCP_SIGNING_KEY"] = LONG_OAUTH_SECRET
        with pytest.raises(ValueError, match="must differ from OAUTH_SESSION_SECRET"):
            build_application_graph(config, UntouchableDb())

    def test_the_graph_refuses_the_placeholder_when_the_oauth_setting_is_unset(self, monkeypatch):
        """The setting absent: AuthConfig falls back to the placeholder, the secret in effect; the key equal to it is
        refused (the raw config value alone would have skipped the comparison)."""
        config, _ = prod_env.prod_settings(monkeypatch)
        del config["OAUTH_SESSION_SECRET"]
        monkeypatch.delenv("OAUTH_SESSION_SECRET", raising=False)
        config["VOICE_MCP_SIGNING_KEY"] = _DEV_DEFAULT_SESSION_SECRET
        with pytest.raises(ValueError, match="public dev placeholder"):
            build_application_graph(config, UntouchableDb())

    def test_the_deployed_shaped_key_is_long_enough(self):
        assert len(prod_env.SECRET_VALUES["VOICE_MCP_SIGNING_KEY"]) >= 48
