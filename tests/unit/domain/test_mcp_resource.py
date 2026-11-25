"""The scope-to-resource binding of the remote MCP server (DRIVE_MCP_CONNECTOR_RFC §3.3, E1)."""

import pytest

from src.domain.mcp_resource import (
    MCPResource,
    ResourceRefused,
    ScopeRefused,
    bind_resource,
    canonical_resource,
    drive_resource_uri,
    is_drive_grant,
    is_memory_inbox_grant,
    memory_inbox_resource_uri,
)

FACTS_SCOPE = "user_context"
DRIVE_SCOPE = "drive_read"
FACTS_URI = "https://mcp.example.test/mcp"
DRIVE_URI = "https://mcp.example.test/drive-mcp"
URIS = {MCPResource.FACTS: FACTS_URI, MCPResource.DRIVE: DRIVE_URI}
SCOPE_OF = {MCPResource.FACTS: FACTS_SCOPE, MCPResource.DRIVE: DRIVE_SCOPE}


def test_canonical_resource_drops_a_trailing_slash_and_lowercases_the_host():
    assert canonical_resource("HTTPS://MCP.Example.Test/mcp/") == "https://mcp.example.test/mcp"
    assert canonical_resource("https://mcp.example.test/") == "https://mcp.example.test/"
    assert canonical_resource("https://mcp.example.test/mcp?x=1#f") == "https://mcp.example.test/mcp"


def test_drive_resource_uri_is_the_facts_host_with_the_drive_path():
    assert drive_resource_uri(FACTS_URI) == DRIVE_URI


@pytest.mark.parametrize("kind, scope", [(MCPResource.FACTS, FACTS_SCOPE), (MCPResource.DRIVE, DRIVE_SCOPE)])
def test_each_resource_with_no_scope_gets_its_own_scope(kind, scope):
    assert bind_resource(URIS[kind] + "/", [], URIS, SCOPE_OF) == (kind, URIS[kind], [scope])


@pytest.mark.parametrize("kind, scope", [(MCPResource.FACTS, FACTS_SCOPE), (MCPResource.DRIVE, DRIVE_SCOPE)])
def test_each_resource_with_its_own_scope_is_bound(kind, scope):
    assert bind_resource(URIS[kind], [scope], URIS, SCOPE_OF) == (kind, URIS[kind], [scope])


@pytest.mark.parametrize("uri, scope", [(FACTS_URI, DRIVE_SCOPE), (DRIVE_URI, FACTS_SCOPE)])
def test_a_scope_of_the_other_resource_is_refused(uri, scope):
    with pytest.raises(ScopeRefused, match="not for resource"):
        bind_resource(uri, [scope], URIS, SCOPE_OF)


@pytest.mark.parametrize("uri", [None, FACTS_URI, DRIVE_URI])
def test_both_scopes_together_are_refused(uri):
    with pytest.raises(ScopeRefused):
        bind_resource(uri, [FACTS_SCOPE, DRIVE_SCOPE], URIS, SCOPE_OF)


@pytest.mark.parametrize("uri", [
    "https://other.example.test/mcp",
    "https://mcp.example.test/mcp-x",
    "https://mcp.example.test/drive-mcp/sub",
    "https://mcp.example.test/mcp/sub",
])
def test_an_unknown_resource_is_refused(uri):
    with pytest.raises(ResourceRefused, match="unknown resource"):
        bind_resource(uri, [], URIS, SCOPE_OF)


def test_an_unknown_resource_with_a_query_that_is_not_the_server_is_refused():
    with pytest.raises(ResourceRefused):
        bind_resource("https://mcp.example.test/mcp-x?r=/mcp", [], URIS, SCOPE_OF)


def test_no_resource_with_the_drive_scope_binds_the_drive():
    assert bind_resource(None, [DRIVE_SCOPE], URIS, SCOPE_OF) == (MCPResource.DRIVE, DRIVE_URI, [DRIVE_SCOPE])
    assert is_drive_grant(DRIVE_URI + "/") and not is_drive_grant(FACTS_URI) and not is_drive_grant(None)


@pytest.mark.parametrize("scopes", [[], [FACTS_SCOPE]])
def test_no_resource_and_no_scope_binds_the_facts_default(scopes):
    assert bind_resource(None, scopes, URIS, SCOPE_OF) == (MCPResource.FACTS, FACTS_URI, [FACTS_SCOPE])


@pytest.mark.parametrize("scopes", [["profile"], [FACTS_SCOPE, "profile"], [DRIVE_SCOPE, "profile"]])
def test_no_resource_with_an_unknown_scope_is_refused(scopes):
    with pytest.raises(ScopeRefused):
        bind_resource(None, scopes, URIS, SCOPE_OF)


# ---------------------------------------------------------------------------------------- the memory inbox (fourth resource)

INBOX_SCOPE = "memory_submit"
INBOX_URI = "https://mcp.example.test/memory-inbox-mcp"
INBOX_SCOPE_OF = {**SCOPE_OF, MCPResource.MEMORY_INBOX: INBOX_SCOPE}
INBOX_URIS = {**URIS, MCPResource.MEMORY_INBOX: INBOX_URI}


def test_the_inbox_uri_is_the_facts_host_with_the_inbox_path():
    assert memory_inbox_resource_uri(FACTS_URI) == INBOX_URI


def test_the_inbox_scope_alone_binds_the_inbox_resource():
    assert bind_resource(None, [INBOX_SCOPE], INBOX_URIS, INBOX_SCOPE_OF) == (
        MCPResource.MEMORY_INBOX, INBOX_URI, [INBOX_SCOPE])
    assert bind_resource(INBOX_URI + "/", [], INBOX_URIS, INBOX_SCOPE_OF) == (
        MCPResource.MEMORY_INBOX, INBOX_URI, [INBOX_SCOPE])


def test_the_inbox_scope_never_mixes_or_rides_another_resource():
    with pytest.raises(ScopeRefused):
        bind_resource(None, [FACTS_SCOPE, INBOX_SCOPE], INBOX_URIS, INBOX_SCOPE_OF)
    with pytest.raises(ScopeRefused):
        bind_resource(FACTS_URI, [INBOX_SCOPE], INBOX_URIS, INBOX_SCOPE_OF)
    with pytest.raises(ScopeRefused):
        bind_resource(INBOX_URI, [FACTS_SCOPE], INBOX_URIS, INBOX_SCOPE_OF)
    with pytest.raises(ScopeRefused):      # not served: no inbox URI
        bind_resource(None, [INBOX_SCOPE], URIS, INBOX_SCOPE_OF)
    with pytest.raises(ScopeRefused):      # unknown: no inbox scope
        bind_resource(None, [INBOX_SCOPE], INBOX_URIS, SCOPE_OF)


def test_only_the_inbox_path_is_an_inbox_grant():
    assert is_memory_inbox_grant(INBOX_URI) and is_memory_inbox_grant(INBOX_URI + "/")
    assert not any(is_memory_inbox_grant(uri) for uri in (FACTS_URI, DRIVE_URI, None, INBOX_URI + "/sub"))
