"""
Firestore-backed storage for MCP OAuth entities.

Three collections, env-prefixed via EnvironmentConfig:
- {env}_mcp_oauth_clients      doc_id = client_id
- {env}_mcp_auth_codes         doc_id = code
- {env}_mcp_refresh_tokens     doc_id = sha256(token_value) hex
"""

from datetime import datetime, timezone
from typing import Optional

from ..config.environment import EnvironmentConfig
from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from ..ports.mcp_client_repository import MCPClientRepository
from ..utils.logger import logger


def _to_utc(value) -> datetime:
    """Normalize any datetime-like from Firestore into a plain tz-aware datetime.

    Firestore returns DatetimeWithNanoseconds; re-constructing a plain datetime
    ensures downstream pydantic / comparison logic behaves predictably.
    """
    return datetime(
        value.year, value.month, value.day,
        value.hour, value.minute, value.second, value.microsecond,
        tzinfo=timezone.utc,
    )


class FirestoreMCPClientRepository(MCPClientRepository):
    def __init__(self, db_client, env_config: EnvironmentConfig):
        self.db = db_client
        self._clients = db_client.collection(env_config.mcp_oauth_clients_collection)
        self._codes = db_client.collection(env_config.mcp_auth_codes_collection)
        self._refresh = db_client.collection(env_config.mcp_refresh_tokens_collection)
        logger.info(
            "📂 MCP client repo initialized "
            f"(clients={env_config.mcp_oauth_clients_collection}, "
            f"codes={env_config.mcp_auth_codes_collection}, "
            f"refresh={env_config.mcp_refresh_tokens_collection})"
        )

    # --- clients ---

    @staticmethod
    def _client_to_fs(c: MCPClient) -> dict:
        return {
            "client_id": c.client_id,
            "client_secret": c.client_secret,
            "client_secret_expires_at": c.client_secret_expires_at,
            "client_name": c.client_name,
            "redirect_uris": list(c.redirect_uris),
            "grant_types": list(c.grant_types),
            "response_types": list(c.response_types),
            "scope": c.scope,
            "token_endpoint_auth_method": c.token_endpoint_auth_method,
            "created_at": c.created_at,
        }

    @staticmethod
    def _client_from_fs(data: dict) -> MCPClient:
        return MCPClient(
            client_id=data["client_id"],
            client_secret=data.get("client_secret"),
            client_secret_expires_at=data.get("client_secret_expires_at"),
            client_name=data.get("client_name", ""),
            redirect_uris=list(data.get("redirect_uris", [])),
            grant_types=list(data.get("grant_types", ["authorization_code", "refresh_token"])),
            response_types=list(data.get("response_types", ["code"])),
            scope=data.get("scope", "user_context"),
            token_endpoint_auth_method=data.get(
                "token_endpoint_auth_method", "client_secret_post"
            ),
            created_at=_to_utc(data["created_at"]),
        )

    async def save_client(self, client: MCPClient) -> None:
        await self._clients.document(client.client_id).set(self._client_to_fs(client))
        logger.debug(f"💾 MCP client saved: {client.client_id}")

    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        doc = await self._clients.document(client_id).get()
        if not doc.exists:
            return None
        return self._client_from_fs(doc.to_dict())

    async def delete_client(self, client_id: str) -> None:
        await self._clients.document(client_id).delete()

    # --- auth codes ---

    @staticmethod
    def _code_to_fs(c: MCPAuthCode) -> dict:
        return {
            "code": c.code,
            "client_id": c.client_id,
            "user_id": c.user_id,
            "account_id": c.account_id,
            "redirect_uri": c.redirect_uri,
            "code_challenge": c.code_challenge,
            "code_challenge_method": c.code_challenge_method,
            "resource": c.resource,
            "scopes": list(c.scopes),
            "expires_at": c.expires_at,
        }

    @staticmethod
    def _code_from_fs(data: dict) -> MCPAuthCode:
        return MCPAuthCode(
            code=data["code"],
            client_id=data["client_id"],
            user_id=data["user_id"],
            account_id=data["account_id"],
            redirect_uri=data["redirect_uri"],
            code_challenge=data["code_challenge"],
            code_challenge_method=data.get("code_challenge_method", "S256"),
            resource=data.get("resource"),
            scopes=list(data.get("scopes", [])),
            expires_at=_to_utc(data["expires_at"]),
        )

    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        await self._codes.document(auth_code.code).set(self._code_to_fs(auth_code))

    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        doc = await self._codes.document(code).get()
        if not doc.exists:
            return None
        return self._code_from_fs(doc.to_dict())

    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        ref = self._codes.document(code)
        doc = await ref.get()
        if not doc.exists:
            return None
        await ref.delete()
        return self._code_from_fs(doc.to_dict())

    # --- refresh tokens ---

    @staticmethod
    def _refresh_to_fs(t: MCPRefreshToken) -> dict:
        return {
            "token_hash": t.token_hash,
            "client_id": t.client_id,
            "user_id": t.user_id,
            "account_id": t.account_id,
            "scopes": list(t.scopes),
            "resource": t.resource,
            "expires_at": t.expires_at,
            "revoked_at": t.revoked_at,
        }

    @staticmethod
    def _refresh_from_fs(data: dict) -> MCPRefreshToken:
        revoked_raw = data.get("revoked_at")
        return MCPRefreshToken(
            token_hash=data["token_hash"],
            client_id=data["client_id"],
            user_id=data["user_id"],
            account_id=data["account_id"],
            scopes=list(data.get("scopes", [])),
            resource=data.get("resource"),
            expires_at=_to_utc(data["expires_at"]),
            revoked_at=_to_utc(revoked_raw) if revoked_raw else None,
        )

    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        await self._refresh.document(token.token_hash).set(self._refresh_to_fs(token))

    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        doc = await self._refresh.document(token_hash).get()
        if not doc.exists:
            return None
        return self._refresh_from_fs(doc.to_dict())

    async def revoke_refresh_token(self, token_hash: str) -> None:
        ref = self._refresh.document(token_hash)
        doc = await ref.get()
        if not doc.exists:
            return
        await ref.update({"revoked_at": datetime.now(timezone.utc)})
