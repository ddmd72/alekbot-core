"""The scope-to-resource binding of the remote MCP server (DRIVE_MCP_CONNECTOR_RFC §3.3, E1)."""

import pytest

from src.domain.mcp_resource import (
    DRIVE_SCOPE,
    FACTS_SCOPE,
    MCPResource,
    ResourceRefused,
    ScopeRefused,
    bind_resource,
    canonical_resource,
    drive_resource_uri,
    is_drive_grant,
)

FACTS_URI = "https://mcp.example.test/mcp"
DRIVE_URI = "https://mcp.example.test/drive-mcp"
URIS = {MCPResource.FACTS: FACTS_URI, MCPResource.DRIVE: DRIVE_URI}


def test_canonical_resource_drops_a_trailing_slash_and_lowercases_the_host():
    assert canonical_resource("HTTPS://MCP.Example.Test/mcp/") == "https://mcp.example.test/mcp"
    assert canonical_resource("https://mcp.example.test/") == "https://mcp.example.test/"
    assert canonical_resource("https://mcp.example.test/mcp?x=1#f") == "https://mcp.example.test/mcp"


def test_drive_resource_uri_is_the_facts_host_with_the_drive_path():
    assert drive_resource_uri(FACTS_URI) == DRIVE_URI


@pytest.mark.parametrize("kind, scope", [(MCPResource.FACTS, FACTS_SCOPE), (MCPResource.DRIVE, DRIVE_SCOPE)])
def test_each_resource_with_no_scope_gets_its_own_scope(kind, scope):
    assert bind_resource(URIS[kind] + "/", [], URIS) == (kind, URIS[kind], [scope])


@pytest.mark.parametrize("kind, scope", [(MCPResource.FACTS, FACTS_SCOPE), (MCPResource.DRIVE, DRIVE_SCOPE)])
def test_each_resource_with_its_own_scope_is_bound(kind, scope):
    assert bind_resource(URIS[kind], [scope], URIS) == (kind, URIS[kind], [scope])


@pytest.mark.parametrize("uri, scope", [(FACTS_URI, DRIVE_SCOPE), (DRIVE_URI, FACTS_SCOPE)])
def test_a_scope_of_the_other_resource_is_refused(uri, scope):
    with pytest.raises(ScopeRefused, match="not for resource"):
        bind_resource(uri, [scope], URIS)


@pytest.mark.parametrize("uri", [None, FACTS_URI, DRIVE_URI])
def test_both_scopes_together_are_refused(uri):
    with pytest.raises(ScopeRefused):
        bind_resource(uri, [FACTS_SCOPE, DRIVE_SCOPE], URIS)


@pytest.mark.parametrize("uri", [
    "https://other.example.test/mcp",
    "https://mcp.example.test/mcp-x",
    "https://mcp.example.test/drive-mcp/sub",
    "https://mcp.example.test/mcp/sub",
])
def test_an_unknown_resource_is_refused(uri):
    with pytest.raises(ResourceRefused, match="unknown resource"):
        bind_resource(uri, [], URIS)


def test_an_unknown_resource_with_a_query_that_is_not_the_server_is_refused():
    with pytest.raises(ResourceRefused):
        bind_resource("https://mcp.example.test/mcp-x?r=/mcp", [], URIS)


def test_no_resource_with_the_drive_scope_binds_the_drive():
    assert bind_resource(None, [DRIVE_SCOPE], URIS) == (MCPResource.DRIVE, DRIVE_URI, [DRIVE_SCOPE])
    assert is_drive_grant([DRIVE_SCOPE]) and not is_drive_grant([FACTS_SCOPE])


@pytest.mark.parametrize("scopes", [[], [FACTS_SCOPE]])
def test_no_resource_and_no_scope_binds_the_facts_default(scopes):
    assert bind_resource(None, scopes, URIS) == (MCPResource.FACTS, FACTS_URI, [FACTS_SCOPE])


@pytest.mark.parametrize("scopes", [["profile"], [FACTS_SCOPE, "profile"], [DRIVE_SCOPE, "profile"]])
def test_no_resource_with_an_unknown_scope_is_refused(scopes):
    with pytest.raises(ScopeRefused):
        bind_resource(None, scopes, URIS)
