"""The voice session's MCP server descriptors and their pairing with the call's tokens (LELIK_VOICE_MCP_RFC §4, S3).

Descriptors carry no token; the relay offers a server only when the call is eligible (plain companion call on xAI) and
the session config holds that server's token.
"""
import json

import pytest

from src.domain.remote_tool_server import RemoteToolServer
from src.domain.voice_mcp_servers import (
    VOICE_MCP_SERVERS_FIELD,
    VOICE_MCP_TOKENS_FIELD,
    session_servers,
    voice_session_servers,
)

XAI = {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}
OPENAI = {"provider": "openai", "turn_ownership": "relay", "voice": "verse", "reasoning_effort": "medium"}
TOKENS = {"facts": "eyJh.facts-token.sig", "drive": "eyJh.drive-token.sig"}


def _config(**over) -> dict:
    config = {"mode": "lelik", "voice": XAI, "user_id": "usr-1", "account_id": "acc-1",
              VOICE_MCP_SERVERS_FIELD: [s.to_dict() for s in voice_session_servers("https://MCP.example.test/mcp/")],
              VOICE_MCP_TOKENS_FIELD: dict(TOKENS)}
    config.update(over)
    return config


class TestDescriptors:
    def test_both_servers_at_the_canonical_resource_uris_with_the_voice_tools(self):
        facts, drive = voice_session_servers("https://MCP.example.test/mcp/")

        assert (facts.label, facts.url, facts.allowed_tools) == (
            "alek_memory", "https://mcp.example.test/mcp", ("get_user_context",))
        assert (drive.label, drive.url, drive.allowed_tools) == (
            "alek_drive", "https://mcp.example.test/drive-mcp", ("search_drive", "find_passages", "read_passages"))
        assert all("never a request" in server.description for server in (facts, drive))
        assert facts.token == "" and drive.token == ""

    def test_a_server_value_never_shows_its_token(self):
        server = voice_session_servers("https://mcp.example.test/mcp")[0].with_token("eyJh.secret.sig")

        assert "secret" not in repr(server) and "secret" not in str(server)
        assert "secret" not in json.dumps(server.to_dict())
        assert RemoteToolServer.from_dict(server.to_dict(), token="t").token == "t"


class TestPairing:
    def test_an_eligible_call_gets_each_server_with_its_own_token(self):
        servers = session_servers(_config())

        assert [(s.label, s.token) for s in servers] == [("alek_memory", TOKENS["facts"]),
                                                         ("alek_drive", TOKENS["drive"])]

    @pytest.mark.parametrize("over", [
        {VOICE_MCP_TOKENS_FIELD: None},
        {VOICE_MCP_SERVERS_FIELD: None},
        {"mode": "interpreter"},
        {"mode": "tutor"},
        {"mode": None},
        {"voice": OPENAI},
        {"voice": None},
    ])
    def test_no_server_without_tokens_descriptors_or_eligibility(self, over):
        assert session_servers(_config(**over)) == ()

    def test_a_server_without_its_token_is_not_offered(self):
        servers = session_servers(_config(**{VOICE_MCP_TOKENS_FIELD: {"facts": TOKENS["facts"]}}))

        assert [s.label for s in servers] == ["alek_memory"]

    def test_a_descriptor_with_a_foreign_label_or_broken_shape_is_skipped(self):
        descriptors = [{"label": "someone_else", "url": "https://x.test", "description": "d", "allowed_tools": []},
                       {"label": "alek_drive"},
                       "not a descriptor"]

        assert session_servers(_config(**{VOICE_MCP_SERVERS_FIELD: descriptors})) == ()
