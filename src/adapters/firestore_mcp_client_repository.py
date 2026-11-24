"""
Firestore-backed storage for MCP OAuth entities (secrets at rest: SECRETS_AT_REST_RFC §2.2, §2.5).

Three collections, env-prefixed via EnvironmentConfig:
- {env}_mcp_clients            doc_id = client_id; client_secret = CipherPort ciphertext, AAD mcp_clients/{client_id}
- {env}_mcp_auth_codes         doc_id = keyed_digest(digest_key, "mcp_code", code); no raw code in any field
- {env}_mcp_refresh_tokens     doc_id = sha256(token_value) hex (unkeyed: 384 random bits gain nothing from a key);
                               `grant_id` (the grant's stable id, not a secret) is a field, queried by
                               has_active_grant with three equality filters (user_id, grant_id, revoked_at null),
                               served by the automatic single-field indexes; `worker_id` (a tasks-resource grant's
                               worker), `granted_at` and `issued_at` are fields too, absent on older documents (read
                               as None); list_worker_grants, revoke_grant and revoke_worker_grants filter on
                               (user_id, worker_id or grant_id, revoked_at null) with equality only, as
                               has_active_grant does; revoke_user_refresh_tokens
                               filters on (user_id, revoked_at null) the same way, so a token stored without a
                               revoked_at field (none since slice 4, all long expired) is not matched

Reads go by the presented value only (port contract): a code is found by digesting the code the caller presents, so a
document keyed by a raw code is never read (§2.7, no legacy branch). A decrypt or cipher failure propagates (I4).
"""

from datetime import datetime, timezone
from typing import List, Optional

from google.cloud import firestore
from google.cloud.firestore import FieldFilter

from ..config.environment import EnvironmentConfig
from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from ..domain.secret_digest import keyed_digest
from ..ports.cipher_port import CipherPort
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


_CLIENT_AAD_STORE = "mcp_clients"
_CODE_PURPOSE = "mcp_code"


class FirestoreMCPClientRepository(MCPClientRepository):
    def __init__(self, db_client, env_config: EnvironmentConfig, *, cipher: CipherPort, digest_key: bytes):
        self.db = db_client
        self._cipher = cipher
        self._digest_key = digest_key
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
    def _client_aad(client_id: str) -> str:
        return f"{_CLIENT_AAD_STORE}/{client_id}"

    @staticmethod
    def _client_to_fs(c: MCPClient, sealed_secret: Optional[str]) -> dict:
        """The stored document; `sealed_secret` is the ciphertext of c.client_secret (None for a public client)."""
        return {
            "client_id": c.client_id,
            "client_secret": sealed_secret,
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
    def _client_from_fs(data: dict, client_id: str, client_secret: Optional[str]) -> MCPClient:
        """Rebuild the client of doc id `client_id`; the identity comes from the record key, never from the body."""
        return MCPClient(
            client_id=client_id,
            client_secret=client_secret,
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
        sealed = None
        if client.client_secret is not None:
            sealed = await self._cipher.encrypt(client.client_secret, aad=self._client_aad(client.client_id))
        await self._clients.document(client.client_id).set(self._client_to_fs(client, sealed))
        logger.debug(f"💾 MCP client saved: {client.client_id}")

    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        doc = await self._clients.document(client_id).get()
        if not doc.exists:
            return None
        data = doc.to_dict()
        # I2 for whole-record copies: the body must belong to this doc id, and the AAD is built from the id the caller
        # asked for, so a document copied under another id never decrypts or reads back as the other client.
        stored_id = data["client_id"]
        if stored_id != client_id:
            logger.error(f"[MCPClientRepo] client doc {client_id} holds the body of another client; refused")
            raise ValueError(f"MCP client document {client_id!r} holds a body for another client id")
        sealed = data.get("client_secret")
        secret = None
        if sealed is not None:
            secret = await self._cipher.decrypt(sealed, aad=self._client_aad(client_id))
        return self._client_from_fs(data, client_id, secret)

    # --- auth codes ---

    def _code_doc_id(self, code: str) -> str:
        return keyed_digest(self._digest_key, _CODE_PURPOSE, code)

    @staticmethod
    def _code_to_fs(c: MCPAuthCode) -> dict:
        """The stored document: every field but the code itself, which only its digest (the doc id) stands for."""
        return {
            "client_id": c.client_id,
            "user_id": c.user_id,
            "account_id": c.account_id,
            "redirect_uri": c.redirect_uri,
            "code_challenge": c.code_challenge,
            "code_challenge_method": c.code_challenge_method,
            "resource": c.resource,
            "scopes": list(c.scopes),
            "expires_at": c.expires_at,
            "worker_id": c.worker_id,
        }

    @staticmethod
    def _code_from_fs(data: dict, code: str) -> MCPAuthCode:
        """Rebuild the code from its stored document and the presented code value."""
        return MCPAuthCode(
            code=code,
            client_id=data["client_id"],
            user_id=data["user_id"],
            account_id=data["account_id"],
            redirect_uri=data["redirect_uri"],
            code_challenge=data["code_challenge"],
            code_challenge_method=data.get("code_challenge_method", "S256"),
            resource=data.get("resource"),
            scopes=list(data.get("scopes", [])),
            expires_at=_to_utc(data["expires_at"]),
            worker_id=data.get("worker_id"),
        )

    async def save_auth_code(self, auth_code: MCPAuthCode) -> None:
        await self._codes.document(self._code_doc_id(auth_code.code)).set(self._code_to_fs(auth_code))

    async def get_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        doc = await self._codes.document(self._code_doc_id(code)).get()
        if not doc.exists:
            return None
        return self._code_from_fs(doc.to_dict(), code)

    async def consume_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        ref = self._codes.document(self._code_doc_id(code))

        # One transaction reads and deletes: of concurrent consumers one commits, the others re-run and read nothing.
        @firestore.async_transactional
        async def _consume(transaction) -> Optional[dict]:
            doc = await ref.get(transaction=transaction)
            if not doc.exists:
                return None
            transaction.delete(ref)
            return doc.to_dict()

        data = await _consume(self.db.transaction())
        return None if data is None else self._code_from_fs(data, code)

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
            "grant_id": t.grant_id,
            "worker_id": t.worker_id,
            "granted_at": t.granted_at,
            "issued_at": t.issued_at,
        }

    @staticmethod
    def _refresh_from_fs(data: dict) -> MCPRefreshToken:
        revoked_raw = data.get("revoked_at")
        granted_raw = data.get("granted_at")
        issued_raw = data.get("issued_at")
        return MCPRefreshToken(
            token_hash=data["token_hash"],
            client_id=data["client_id"],
            user_id=data["user_id"],
            account_id=data["account_id"],
            scopes=list(data.get("scopes", [])),
            resource=data.get("resource"),
            expires_at=_to_utc(data["expires_at"]),
            revoked_at=_to_utc(revoked_raw) if revoked_raw else None,
            grant_id=data.get("grant_id"),
            worker_id=data.get("worker_id"),
            granted_at=_to_utc(granted_raw) if granted_raw else None,
            issued_at=_to_utc(issued_raw) if issued_raw else None,
        )

    async def save_refresh_token(self, token: MCPRefreshToken) -> None:
        await self._refresh.document(token.token_hash).set(self._refresh_to_fs(token))

    async def get_refresh_token(self, token_hash: str) -> Optional[MCPRefreshToken]:
        doc = await self._refresh.document(token_hash).get()
        if not doc.exists:
            return None
        return self._refresh_from_fs(doc.to_dict())

    async def revoke_refresh_token(self, token_hash: str) -> bool:
        ref = self._refresh.document(token_hash)

        # One transaction reads and revokes: of concurrent revokers of an active token exactly one sees it active.
        @firestore.async_transactional
        async def _revoke(transaction) -> bool:
            doc = await ref.get(transaction=transaction)
            if not doc.exists:
                return False
            was_active = not doc.to_dict().get("revoked_at")
            transaction.update(ref, {"revoked_at": datetime.now(timezone.utc)})
            return was_active

        revoked: bool = await _revoke(self.db.transaction())
        return revoked

    async def revoke_user_refresh_tokens(self, user_id: str, now: datetime) -> int:
        # Two equality filters (user_id, revoked_at null): the automatic single-field indexes serve them. One
        # transaction reads them all, then writes: a concurrent revoke of one of them makes this re-run.
        query = (
            self._refresh.where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("revoked_at", "==", None))
        )

        @firestore.async_transactional
        async def _revoke_all(transaction) -> int:
            snaps = [s for s in await query.get(transaction=transaction) if s.to_dict().get("user_id") == user_id]
            for snap in snaps:
                transaction.update(snap.reference, {"revoked_at": now})
            return len(snaps)

        revoked: int = await _revoke_all(self.db.transaction())
        logger.info(f"🔒 MCP refresh tokens revoked: user={user_id[:8]} count={revoked}")
        return revoked

    async def has_active_grant(self, user_id: str, grant_id: str, now: datetime) -> bool:
        # One grant has one unrevoked token at a time (rotation revokes the old one); the limit only bounds a
        # pathological case. A token stored with no revoked_at field never carries a grant_id (pre-slice-4).
        # Equality filters only: served by merging the automatic single-field indexes, no composite index.
        query = (
            self._refresh.where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter("grant_id", "==", grant_id))
            .where(filter=FieldFilter("revoked_at", "==", None))
            .limit(5)
        )
        docs = await query.get()
        return any(_to_utc(doc.to_dict()["expires_at"]) > now for doc in docs)

    # --- the task queue's grants, per worker ---

    def _unrevoked(self, user_id: str, field: str, value: str):
        """Unrevoked tokens of `user_id` with `field` == `value`: three equality filters, served by merging the
        automatic single-field indexes (no composite index)."""
        return (
            self._refresh.where(filter=FieldFilter("user_id", "==", user_id))
            .where(filter=FieldFilter(field, "==", value))
            .where(filter=FieldFilter("revoked_at", "==", None))
        )

    async def list_worker_grants(self, user_id: str, worker_id: str, now: datetime) -> List[MCPRefreshToken]:
        tokens = [self._refresh_from_fs(doc.to_dict()) for doc in await self._unrevoked(user_id, "worker_id",
                                                                                         worker_id).get()]
        return [t for t in tokens if t.user_id == user_id and t.worker_id == worker_id and t.expires_at > now]

    async def _revoke_matching(self, user_id: str, field: str, value: str, now: datetime) -> int:
        query = self._unrevoked(user_id, field, value)

        # One transaction reads them all, then writes: a concurrent rotation that revokes one of them makes this re-run.
        @firestore.async_transactional
        async def _revoke_all(transaction) -> int:
            snaps = [s for s in await query.get(transaction=transaction)
                     if s.to_dict().get("user_id") == user_id and s.to_dict().get(field) == value]
            for snap in snaps:
                transaction.update(snap.reference, {"revoked_at": now})
            return len(snaps)

        revoked: int = await _revoke_all(self.db.transaction())
        return revoked

    async def revoke_grant(self, user_id: str, grant_id: str, now: datetime) -> int:
        revoked = await self._revoke_matching(user_id, "grant_id", grant_id, now)
        logger.info(f"🔒 MCP grant revoked: user={user_id[:8]} count={revoked}")
        return revoked

    async def revoke_worker_grants(self, user_id: str, worker_id: str, now: datetime) -> int:
        revoked = await self._revoke_matching(user_id, "worker_id", worker_id, now)
        logger.info(f"🔒 MCP worker grants revoked: user={user_id[:8]} count={revoked}")
        return revoked
