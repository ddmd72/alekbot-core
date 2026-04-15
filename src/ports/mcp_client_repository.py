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

    # --- clients (RFC 7591 DCR) ---

    @abstractmethod
    async def save_client(self, client: MCPClient) -> None: ...

    @abstractmethod
    async def get_client(self, client_id: str) -> Optional[MCPClient]: ...

    @abstractmethod
    async def delete_client(self, client_id: str) -> None: ...

    # --- authorization codes (one-shot) ---

    @abstractmethod
    async def save_auth_code(self, auth_code: MCPAuthCode) -> None: ...

    @abstractmethod
    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Load the code without consuming it. Returns None if absent."""

    @abstractmethod
    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Atomically load and delete the code. Returns None if absent."""

    # --- refresh tokens (rotated) ---

    @abstractmethod
    async def save_refresh_token(self, token: MCPRefreshToken) -> None: ...

    @abstractmethod
    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]: ...

    @abstractmethod
    async def revoke_refresh_token(self, token_hash: str) -> None:
        """Mark the token as revoked (sets revoked_at=now). Idempotent."""
