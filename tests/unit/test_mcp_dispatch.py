"""main.mcp_owner: which MCP app (or the Quart app) a request path goes to."""

import pytest

import main


@pytest.mark.parametrize("path", [
    "/drive-mcp", "/drive-mcp/", "/.well-known/oauth-protected-resource/drive-mcp",
])
def test_drive_paths_go_to_the_drive_app(path):
    assert main.mcp_owner(path) == "drive"


@pytest.mark.parametrize("path", [
    "/mcp", "/mcp/", "/authorize", "/token", "/register", "/revoke",
    "/.well-known/oauth-authorization-server", "/.well-known/oauth-protected-resource/mcp",
])
def test_facts_and_authorization_server_paths_go_to_the_facts_app(path):
    assert main.mcp_owner(path) == "facts"


@pytest.mark.parametrize("path", ["/mcp/consent", "/cabinet", "/drive-mcpx", "/drive-mcp/sub", "/health"])
def test_everything_else_goes_to_the_quart_app(path):
    assert main.mcp_owner(path) is None


def test_the_drive_prm_wins_over_the_facts_prefix():
    path = "/.well-known/oauth-protected-resource/drive-mcp"
    assert main._is_mcp_path(path)          # the facts matcher is a prefix match and would take it
    assert main.mcp_owner(path) == "drive"
