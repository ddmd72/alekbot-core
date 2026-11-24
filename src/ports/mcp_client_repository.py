"""
Storage contract for MCP OAuth entities (clients, auth codes, refresh tokens).

Implemented by FirestoreMCPClientRepository. Used by MCPAuthorizationService.

Access tokens are NOT persisted — they are stateless JWTs signed with
`auth_config.oauth_session_secret`, verified by the service on every
MCP request.
"""

from abc import ABC, abstractmethod
from datetime import datetime
from typing import List, Optional

from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken


class MCPClientRepository(ABC):
    """
    Port for the MCP OAuth server's stored entities: DCR clients, one-shot authorization codes and rotated refresh
    tokens (keyed by the sha256 hash of the token value).

    Holds for every method:
    - Tenancy: reads are by key only (client_id, code, token hash), with no client or user scope, except the
      per-worker grant reads and revocations at the end, which are scoped to one user. The caller
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

    # --- authorization codes (one-shot) ---

    @abstractmethod
    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        """Store `auth_code` under auth_code.code; a code with that value is replaced."""

    @abstractmethod
    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Return the code without consuming it; None when absent or already consumed.

        Legacy: a missing worker_id reads as None (a code stored before the task queue's grants).
        """

    @abstractmethod
    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Delete the code and return it; None when absent or already consumed.

        Atomicity: one-shot (mcp_authorization_service.py:finalize_auth_code): of concurrent calls on one code
            exactly one returns it.
        """

    # --- refresh tokens (rotated) ---

    @abstractmethod
    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        """Store `token` under token.token_hash; a token with that hash is replaced."""

    @abstractmethod
    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        """Return the token, revoked ones included (revoked_at set); None when absent.

        The caller refuses a revoked token (mcp_authorization_service.py:314).
        Legacy: a missing revoked_at reads as None, i.e. active (observed); a missing grant_id, worker_id,
        granted_at or issued_at reads as None.
        """

    @abstractmethod
    async def revoke_refresh_token(self, token_hash: str) -> bool:
        """Set revoked_at to now; get_refresh_token then returns it revoked. True when this call revoked an active
        token, False when the hash is absent or the token was already revoked.

        Idempotency: an absent hash is a no-op; revoking again moves revoked_at to now (observed).
        Atomicity: rotation allows one exchange per token (mcp_authorization_service.py:rotate_refresh_token): of
            concurrent calls on one active token exactly one returns True.
        """

    @abstractmethod
    async def revoke_user_refresh_tokens(self, user_id: str, now: datetime) -> int:
        """Set revoked_at to `now` on every refresh token of `user_id` that is not revoked yet, in one transaction, and
        return how many it revoked: every MCP connector grant of the user ends at its next refresh
        (CABINET_DEVICE_SESSIONS_RFC §7.6 kill switch, §7.8 account lock). Tokens of other users are untouched; a token
        already revoked keeps its revoked_at. An access token already issued lives to its expiry.

        Race: a rotation that revoked its old token before this call and saves its new one after it leaves the new one
        active; the account lock's `is_active` check at rotation closes that path.
        """

    @abstractmethod
    async def has_active_grant(self, user_id: str, grant_id: str, now: datetime) -> bool:
        """True when some refresh token of `user_id` with that `grant_id` and `revoked_at` null has `expires_at` > now.

        The one-time drive link re-checks its grant with this at fetch (DRIVE_MCP_CONNECTOR_RFC §3.3.8, §8).
        A revoked or expired grant answers False; an unknown grant id answers False.
        """

    # --- the task queue's grants, per worker (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth", §5.6) ---

    @abstractmethod
    async def list_worker_grants(self, user_id: str, worker_id: str, now: datetime) -> List[MCPRefreshToken]:
        """Every refresh token of `user_id` with that `worker_id`, `revoked_at` null and `expires_at` > now: the live
        tokens of the worker's OAuth grants (one per grant, as rotation revokes the old one). Tokens of other users and
        of other workers are never returned; an unknown worker answers []. Order is unspecified."""

    @abstractmethod
    async def revoke_grant(self, user_id: str, grant_id: str, now: datetime) -> int:
        """Set revoked_at to `now` on every refresh token of `user_id` with that `grant_id` that is not revoked yet, in
        one transaction, and return how many it revoked; the grant then ends at its next refresh, and `has_active_grant`
        answers False for it at once. Other grants and other users' tokens are untouched; an unknown grant revokes 0."""

    @abstractmethod
    async def revoke_worker_grants(self, user_id: str, worker_id: str, now: datetime) -> int:
        """`revoke_grant` for every grant of `user_id` with that `worker_id`, in one transaction: every unrevoked token
        of the worker's grants gets revoked_at `now`. Returns how many it revoked; other workers' and other users'
        tokens are untouched."""
