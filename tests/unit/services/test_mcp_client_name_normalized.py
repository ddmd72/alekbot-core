"""
A dynamically registered client's name is stored normalized: no control characters (it is logged, and a line break
would forge a log line), whitespace collapsed, at most CLIENT_NAME_MAX characters. Registration is anonymous, so the
name is whatever the registrant chose; a normal name is stored as given.
"""

from datetime import datetime, timezone

import pytest

from src.domain.mcp import CLIENT_NAME_MAX, MCPClient, normalize_client_name

from .test_mcp_authorization_service import InMemoryMCPRepo, _TEST_ALLOWED_HOSTS, _TEST_RESOURCE_URI, _TEST_SECRET
from src.services.mcp_authorization_service import MCPAuthorizationService


@pytest.mark.parametrize("raw, expected", [
    ("Claude", "Claude"),
    ("  Claude   Desktop ", "Claude Desktop"),
    ("Evil\nINFO forged line\r\n", "Evil INFO forged line"),
    ("tab\there\x00nul\x1b[31m", "tab here nul [31m"),
    ("", ""),
    (None, ""),
])
def test_normalize_client_name(raw, expected):
    assert normalize_client_name(raw) == expected


def test_long_name_is_cut():
    assert len(normalize_client_name("a" * 5000)) == CLIENT_NAME_MAX


async def test_save_client_stores_the_normalized_name():
    repo = InMemoryMCPRepo()
    service = MCPAuthorizationService(
        repo=repo, jwt_secret=_TEST_SECRET, mcp_resource_uri=_TEST_RESOURCE_URI,
        consent_base_url="https://dev.alekbot.app/mcp/consent", allowed_redirect_hosts=_TEST_ALLOWED_HOSTS,
        access_token_ttl=3600, refresh_token_ttl=2592000, auth_code_ttl=600, consent_request_ttl=600,
    )
    await service.save_client(MCPClient(
        client_id="mcp-x", client_secret="s", client_name="Evil\nINFO forged" + "x" * 500,
        redirect_uris=["https://claude.ai/api/mcp/auth_callback"], created_at=datetime.now(timezone.utc),
    ))
    stored = repo.clients["mcp-x"].client_name
    assert "\n" not in stored and stored.startswith("Evil INFO forged") and len(stored) == CLIENT_NAME_MAX
