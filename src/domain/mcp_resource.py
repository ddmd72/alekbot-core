"""
The two protected resources of the remote MCP server and the rule that binds a scope to its resource.

One authorization server fronts two resource servers: the facts connector (`/mcp`, scope `user_context`) and the
drive connector (`/drive-mcp`, scope `drive_read`). A token must never carry an audience its scope does not belong
to (DRIVE_MCP_CONNECTOR_RFC §3.3, E1). Pure: stdlib only.
"""

from enum import Enum
from typing import List, Mapping, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit


class MCPResource(str, Enum):
    FACTS = "facts"
    DRIVE = "drive"


FACTS_SCOPE = "user_context"
DRIVE_SCOPE = "drive_read"
DRIVE_PATH = "/drive-mcp"

SCOPE_OF: Mapping[MCPResource, str] = {
    MCPResource.FACTS: FACTS_SCOPE,
    MCPResource.DRIVE: DRIVE_SCOPE,
}


class ResourceRefused(ValueError):
    """The `resource` indicator is not one of the server's resources."""


class ScopeRefused(ValueError):
    """A requested scope does not belong to the resource, or the scopes mix resources."""


def canonical_resource(uri: str) -> str:
    """Lowercase scheme and host, path without a trailing slash (but "/" stays), no query or fragment."""
    parts = urlsplit(uri.strip())
    path = parts.path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, "", ""))


def drive_resource_uri(facts_resource_uri: str) -> str:
    """Same scheme and host as the facts URI, path DRIVE_PATH (E4: no new deploy key)."""
    parts = urlsplit(facts_resource_uri.strip())
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), DRIVE_PATH, "", ""))


def bind_resource(
    resource: Optional[str],
    scopes: Sequence[str],
    uris: Mapping[MCPResource, str],
) -> Tuple[MCPResource, str, List[str]]:
    """Return (kind, canonical uri, bound scopes).

    `resource` given: it must canonicalise to one of `uris` (else ResourceRefused); every requested scope must be
    that resource's own (else ScopeRefused); none requested -> the resource's own scope.
    `resource` absent: DRIVE when the scopes are exactly {drive_read}; FACTS when they are empty or exactly
    {user_context}; anything else (a mix, an unknown scope) -> ScopeRefused.
    """
    requested = list(scopes)
    if resource is not None:
        canon = canonical_resource(resource)
        kind = next((k for k, u in uris.items() if canonical_resource(u) == canon), None)
        if kind is None:
            raise ResourceRefused("unknown resource")
        own = SCOPE_OF[kind]
        if any(s != own for s in requested):
            raise ScopeRefused(f"scope not for resource {kind.value}")
        return kind, canonical_resource(uris[kind]), [own]

    unique = set(requested)
    if unique == {DRIVE_SCOPE}:
        kind = MCPResource.DRIVE
    elif not unique or unique == {FACTS_SCOPE}:
        kind = MCPResource.FACTS
    else:
        raise ScopeRefused("scope not for resource: unknown or mixed scopes")
    return kind, canonical_resource(uris[kind]), [SCOPE_OF[kind]]


def is_drive_grant(scopes: Sequence[str]) -> bool:
    """The consent page's text choice: the grant carries the drive scope."""
    return DRIVE_SCOPE in scopes
