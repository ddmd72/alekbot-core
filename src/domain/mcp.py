"""
MCP (Model Context Protocol) domain models for the remote MCP server
exposed to Anthropic's claude.ai Custom Connectors.

Pure domain: no infrastructure, no SDK imports. Translation to `mcp` SDK
types happens in `src/adapters/mcp_sdk_oauth_provider.py`.

Three entities correspond to the three Firestore collections:
- MCPClient: DCR-registered OAuth client (claude.ai registers itself)
- MCPAuthCode: one-shot authorization code issued after user consent
- MCPRefreshToken: long-lived refresh token, rotated on every use

Plus the argument normalizers for the `get_user_context` tool — pure
functions so the coercion rules are testable without the SDK.
"""

import json
import re
from datetime import datetime
from typing import Any, List, Optional

from pydantic import BaseModel, Field


# Maximum topical tags fed to the keyword retrieval vector. Excess tags
# are dropped silently rather than rejected — see normalize_keywords.
KEYWORDS_MAX = 5

# Separators a model might use inside a single keyword string.
_KEYWORD_SEPARATORS = re.compile(r"[,;/|\s]+")

# Punctuation left over when a model hand-rolls a list into a string,
# e.g. '["mcp"' -> 'mcp'.
_KEYWORD_STRIP_CHARS = "\"'[]{}()"


def normalize_phrase(value: Any) -> str:
    """
    Coerce a free-text tool argument into a trimmed string.

    `None` becomes `""` (which disables the corresponding retrieval
    vector) rather than raising: a client that collapses an optional
    parameter to an explicit null must not cost a round-trip.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()


def normalize_keywords(value: Any) -> List[str]:
    """
    Coerce whatever a client sent into at most KEYWORDS_MAX lowercase,
    whitespace-free topical tags.

    Accepts: None, a list, a JSON-encoded list, a delimited string, a
    bare string, or any scalar. Every branch returns a valid list —
    there is deliberately no rejection path, because a schema violation
    surfaces to the calling model as a failed tool call and buys nothing
    that silent coercion doesn't.

    Excess tags beyond KEYWORDS_MAX are dropped, duplicates removed,
    order preserved.
    """
    items = _as_item_list(value)

    tags: List[str] = []
    seen = set()
    for item in items:
        text = item if isinstance(item, str) else str(item)
        for token in _KEYWORD_SEPARATORS.split(text.strip().lower()):
            token = token.strip(_KEYWORD_STRIP_CHARS)
            if not token or token in seen:
                continue
            seen.add(token)
            tags.append(token)
            if len(tags) == KEYWORDS_MAX:
                return tags
    return tags


def _as_item_list(value: Any) -> List[Any]:
    """Unwrap a keywords argument into a flat list of candidate items."""
    if value is None:
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except ValueError:
            return [value]
        return parsed if isinstance(parsed, list) else [parsed]
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


class MCPClient(BaseModel):
    """
    OAuth 2.1 confidential client registered via RFC 7591 DCR.

    client_secret is stored in plaintext because the `mcp` SDK's token
    handler performs client authentication via
    `hmac.compare_digest(client.client_secret, presented_secret)` —
    there is no hashing hook in the SDK. Access to Firestore is
    restricted by the service account; treat the collection as
    sensitive and never expose it.
    """
    client_id: str
    client_secret: Optional[str] = None  # None only if token_endpoint_auth_method == "none"
    client_secret_expires_at: Optional[int] = None
    client_name: str = ""
    redirect_uris: List[str]
    grant_types: List[str] = Field(default_factory=lambda: ["authorization_code", "refresh_token"])
    response_types: List[str] = Field(default_factory=lambda: ["code"])
    scope: str = ""
    token_endpoint_auth_method: str = "client_secret_post"
    created_at: datetime


class MCPAuthCode(BaseModel):
    """
    Short-lived (10 min) authorization code. Consumed exactly once on
    /token exchange. Carries the PKCE challenge, the bound redirect_uri,
    the requested resource (RFC 8707), and the user/account that
    authorized this code.
    """
    code: str
    client_id: str
    user_id: str
    account_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str = "S256"
    resource: Optional[str] = None
    scopes: List[str] = Field(default_factory=list)
    expires_at: datetime


class MCPRefreshToken(BaseModel):
    """
    Refresh token. Stored by sha256 hash of the opaque token value.
    Rotated on every /token exchange: the old token is marked revoked
    and a new one is issued.
    """
    token_hash: str
    client_id: str
    user_id: str
    account_id: str
    scopes: List[str] = Field(default_factory=list)
    resource: Optional[str] = None
    expires_at: datetime
    revoked_at: Optional[datetime] = None

    @property
    def is_active(self) -> bool:
        return self.revoked_at is None
