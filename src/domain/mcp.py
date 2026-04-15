"""
MCP (Model Context Protocol) domain models for the remote MCP server
exposed to Anthropic's claude.ai Custom Connectors.

Pure domain: no infrastructure, no SDK imports. Translation to `mcp` SDK
types happens in `src/adapters/mcp_sdk_oauth_provider.py`.

Three entities correspond to the three Firestore collections:
- MCPClient: DCR-registered OAuth client (claude.ai registers itself)
- MCPAuthCode: one-shot authorization code issued after user consent
- MCPRefreshToken: long-lived refresh token, rotated on every use
"""

from datetime import datetime
from typing import List, Optional

from pydantic import BaseModel, Field


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
