"""
Storage contract for MCP OAuth entities (clients, auth codes, refresh tokens).

Implemented by FirestoreMCPClientRepository. Used by MCPAuthorizationService.

Access tokens are NOT persisted — they are stateless JWTs signed with
`auth_config.oauth_session_secret`, verified by the service on every
MCP request.
"""

from abc import ABC, abstractmethod
from typing import Optional

from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken


class MCPClientRepository(ABC):
    """
    Port for the MCP OAuth server's stored entities: DCR clients, one-shot authorization codes and rotated refresh
    tokens (keyed by the sha256 hash of the token value).

    Holds for every method:
    - Tenancy: reads are by key only (client_id, code, token hash), with no client or user scope. The caller
      refuses a code or token of another client by comparing the returned client_id
      (mcp_sdk_oauth_provider.py:190,236), so every read returns client_id, user_id and account_id exactly as saved.
    - Expiry: reads return expired codes and tokens; expires_at reads back tz-aware UTC and the SDK token handler
      refuses an expired one. Nothing is deleted on expiry (observed).
    - Legacy documents: a document missing a required model field raises on read (observed).
    - Errors: backend errors propagate; the token endpoint then fails, never grants.
    """

    # --- clients (RFC 7591 DCR) ---

    @abstractmethod
    async def save_client(self, client: MCPClient) -> None:
        """Store `client` under client.client_id; get_client returns every field as saved.

        Idempotency: a client with that id is replaced.
        """

    @abstractmethod
    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        """Return the client, or None when absent (an unknown client is refused).

        Legacy: missing redirect_uris reads as [] (the SDK client model then fails to build: refused); missing
            grant_types, response_types or token_endpoint_auth_method read as the model defaults; missing scope reads
            as "user_context" (observed).
        """

    @abstractmethod
    async def delete_client(self, client_id: str) -> None:
        """Delete the client.

        Contract (observed; no caller in src/).
        Idempotency: an absent id is a no-op. The client's codes and refresh tokens stay.
        """

    # --- authorization codes (one-shot) ---

    @abstractmethod
    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        """Store `auth_code` under auth_code.code; a code with that value is replaced."""

    @abstractmethod
    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Return the code without consuming it; None when absent or already consumed."""

    @abstractmethod
    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Delete the code and return it; None when absent or already consumed.

        Atomicity: one-shot (mcp_authorization_service.py:270): of concurrent calls on one code exactly one returns
            it. Known defect: the adapter reads, then deletes, outside a transaction, so concurrent calls can each
            return it (firestore_mcp_client_repository.py:133-139).
        """

    # --- refresh tokens (rotated) ---

    @abstractmethod
    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        """Store `token` under token.token_hash; a token with that hash is replaced."""

    @abstractmethod
    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        """Return the token, revoked ones included (revoked_at set); None when absent.

        The caller refuses a revoked token (mcp_authorization_service.py:314).
        Legacy: a missing revoked_at reads as None, i.e. active (observed).
        """

    @abstractmethod
    async def revoke_refresh_token(self, token_hash: str) -> None:
        """Set revoked_at to now; get_refresh_token then returns it revoked.

        Idempotency: an absent hash is a no-op; revoking again moves revoked_at to now (observed).
        Known defect: rotation needs one exchange per token, but get and revoke are separate calls and revoke does
            not say whether it revoked an active token, so two concurrent exchanges of one refresh token both get
            new tokens (mcp_authorization_service.py:309-332).
        """
