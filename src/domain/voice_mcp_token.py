"""The voice MCP access token kind (LELIK_VOICE_MCP_RFC §5): what tells it apart from the OAuth one, what it may call,
the live-call record that keeps it alive, and the call shape that gets one.

A voice token is a per-call, read-only, user-bound HS256 JWT under its own key (`VOICE_MCP_SIGNING_KEY`): header `kid`
`VOICE_MCP_KID`, `type` `VOICE_MCP_TOKEN_TYPE`, audience `voice:` + the canonical resource URI (never an OAuth
audience), a `tools` claim and a `cid` claim, the keyed digest of the call's ticket. It verifies only while the
live-call record `voice_call_key(cid)` exists; `/voice/submit-transcript` deletes it at call end. Pure: stdlib + domain.
"""
from enum import Enum
from typing import Mapping, Optional, Sequence, Tuple

from src.domain.drive_tool_texts import FIND_PASSAGES, READ_PASSAGES, SEARCH_DRIVE
from src.domain.mcp_resource import MCPResource, canonical_resource
from src.domain.secret_digest import keyed_digest
from src.domain.voice_mode import VoiceMode

# The unverified header value that routes a bearer token to the voice verifier; any other value goes to the OAuth one.
VOICE_MCP_KID = "voice-mcp"
VOICE_MCP_TOKEN_TYPE = "voice_mcp_access"
# The SDK's AccessToken needs a client id; it is also the `client=voice` value of every log line and span.
VOICE_MCP_CLIENT = "voice"
# The relay's 60-minute timeout is the only hard call cap (§5.2): the token and its live-call record live as long.
VOICE_MCP_TOKEN_TTL_S = 3600
# A voice token's drive tool results are cut here instead of the connector's 100 000 characters (§8, O5).
VOICE_RESULT_MAX_CHARS = 8000
# The provider whose session carries the MCP tools (O6).
VOICE_MCP_PROVIDER = "xai"
# The tools each resource's voice token carries (§4, O2): index reads only. Only the drive resource has a voice token:
# memory reaches a call by delegation (RFC §2 amendment, 2026-11-10).
VOICE_ALLOWED_TOOLS: Mapping[MCPResource, Tuple[str, ...]] = {
    MCPResource.DRIVE: (SEARCH_DRIVE, FIND_PASSAGES, READ_PASSAGES),
}

_AUDIENCE_PREFIX = "voice:"
_CID_PURPOSE = "voice_mcp"


class McpTokenKind(str, Enum):
    """Which verifier accepted an MCP bearer token. OAUTH is the default: anything not a verified voice token."""

    OAUTH = "oauth"
    VOICE = "voice"


def voice_audience(resource_uri: str) -> str:
    """`voice:` + the canonical form of `resource_uri`: never equal to an OAuth audience."""
    return _AUDIENCE_PREFIX + canonical_resource(resource_uri)


def voice_call_id(digest_key: bytes, ticket: str) -> str:
    """The `cid` claim: the keyed digest of the call's ticket under purpose `voice_mcp`; never the raw ticket."""
    return keyed_digest(digest_key, _CID_PURPOSE, ticket)


def voice_call_key(cid: str) -> str:
    """The live-call record's store key; the record holds `{"user_id"}` and nothing else."""
    return f"voice_mcp_call:{cid}"


def voice_mcp_eligible(mode: Optional[str], provider: Optional[str]) -> bool:
    """True only for the plain companion call (O7) on xAI (O6). An unknown or missing value is not eligible."""
    return mode == VoiceMode.LELIK.value and provider == VOICE_MCP_PROVIDER


def tool_permitted(kind: McpTokenKind, tools: Optional[Sequence[str]], tool: str) -> bool:
    """An OAuth token without a tools claim may call every tool of its resource; any other token only the tools its
    claim names (a voice token without a claim may call none)."""
    if kind is McpTokenKind.OAUTH and tools is None:
        return True
    return tools is not None and tool in tools
