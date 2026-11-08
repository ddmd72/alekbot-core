"""The voice MCP token kind's pure rules (LELIK_VOICE_MCP_RFC §4, §5, O2, O6, O7)."""
import pytest

from src.domain.mcp_resource import MCPResource
from src.domain.secret_digest import keyed_digest
from src.domain.voice_mcp_token import (
    VOICE_ALLOWED_TOOLS,
    McpTokenKind,
    tool_permitted,
    voice_audience,
    voice_call_id,
    voice_call_key,
    voice_mcp_eligible,
)

KEY = bytes(range(32))


def test_the_voice_audience_is_never_an_oauth_audience():
    assert voice_audience("HTTPS://MCP.Example.Test/mcp/") == "voice:https://mcp.example.test/mcp"


def test_the_cid_is_the_keyed_digest_and_the_key_holds_no_ticket():
    cid = voice_call_id(KEY, "ticket-0001-invented")
    assert cid == keyed_digest(KEY, "voice_mcp", "ticket-0001-invented")
    assert cid != keyed_digest(KEY, "voice_ticket", "ticket-0001-invented")
    assert voice_call_key(cid) == f"voice_mcp_call:{cid}" and "ticket" not in voice_call_key(cid)


def test_the_allowed_tools_are_index_reads_only():
    assert VOICE_ALLOWED_TOOLS == {MCPResource.FACTS: ("get_user_context",),
                                   MCPResource.DRIVE: ("search_drive", "find_passages", "read_passages")}


@pytest.mark.parametrize("mode, provider, eligible", [
    ("lelik", "xai", True),
    ("interpreter", "xai", False),
    ("tutor", "xai", False),
    ("lelik", "openai", False),
    (None, "xai", False),
    ("lelik", None, False),
])
def test_only_the_companion_call_on_xai_is_eligible(mode, provider, eligible):
    assert voice_mcp_eligible(mode, provider) is eligible


@pytest.mark.parametrize("kind, tools, tool, permitted", [
    (McpTokenKind.OAUTH, None, "open_drive_file", True),
    (McpTokenKind.OAUTH, ["search_drive"], "open_drive_file", False),
    (McpTokenKind.VOICE, ["search_drive"], "search_drive", True),
    (McpTokenKind.VOICE, ["search_drive"], "open_drive_file", False),
    (McpTokenKind.VOICE, None, "search_drive", False),
    (McpTokenKind.VOICE, [], "search_drive", False),
])
def test_a_tool_is_permitted_by_kind_and_claim(kind, tools, tool, permitted):
    assert tool_permitted(kind, tools, tool) is permitted
