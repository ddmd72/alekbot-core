"""The voice MCP token kind's test kit (LELIK_VOICE_MCP_RFC §5, slice S1).

The real `VoiceMcpTokenService` over the real `FirestoreEphemeralStore` on the evaluating fake, wrapped so a test sees
which keys were read; the real `MCPAuthorizationService` (for OAuth tokens to present at the voice verifier and the
other way round); invented ids only (`usr-1`, `acc-1`).
"""
from types import SimpleNamespace
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import jwt

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.composition.mcp_setup import SCOPE_OF
from src.domain.mcp_resource import MCPResource, drive_resource_uri
from src.ports.ephemeral_store import EphemeralStore
from src.services.mcp_authorization_service import MCPAuthorizationService
from src.services.voice_mcp_token_service import VoiceMcpTokenService
from tests.support.firestore_eval import EvalDb
from tests.support.mcp_gate_fakes import allowing_repos

BASE = "https://mcp.example.test"
FACTS_URI = f"{BASE}/mcp"
DRIVE_URI = drive_resource_uri(FACTS_URI)
URIS = {MCPResource.FACTS: FACTS_URI, MCPResource.DRIVE: DRIVE_URI}
VOICE_KEY = "voice-signing-key-0123456789abcdef-test"
OAUTH_SECRET = "oauth-session-secret-0123456789abcdef-test"
DIGEST_KEY = bytes(range(32))
USER, ACCOUNT, OTHER_USER = "usr-1", "acc-1", "usr-2"
TICKET = "ticket-0001-invented"
COLLECTION = "voice_tickets"
AUTH_CONFIG = SimpleNamespace(mcp_resource_uri=FACTS_URI, oauth_session_secret=OAUTH_SECRET)


class ReadRecordingStore(EphemeralStore):
    """The real store, plus the keys read through it."""

    def __init__(self, inner: EphemeralStore) -> None:
        self.inner = inner
        self.reads: List[str] = []

    async def set(self, key: str, value: Dict[str, Any], ttl_s: int) -> None:
        await self.inner.set(key, value, ttl_s)

    async def get(self, key: str) -> Optional[Dict[str, Any]]:
        self.reads.append(key)
        return await self.inner.get(key)

    async def delete(self, key: str) -> None:
        await self.inner.delete(key)

    async def get_and_delete(self, key: str) -> Optional[Dict[str, Any]]:
        self.reads.append(key)
        return await self.inner.get_and_delete(key)


class Alerts:
    def __init__(self) -> None:
        self.posted: List[str] = []

    async def post(self, text: str) -> None:
        self.posted.append(text)


class Kit:
    """One world: the store on one EvalDb, the voice service, the OAuth service, the alerts."""

    def __init__(self, *, users: Optional[tuple] = None, clock=None, db: Optional[EvalDb] = None) -> None:
        self.db = db or EvalDb()
        self.store = ReadRecordingStore(FirestoreEphemeralStore(self.db, collection=COLLECTION))
        self.user_repo, self.whitelist_repo = allowing_repos(*(users if users is not None else (USER, OTHER_USER)))
        self.alerts = Alerts()
        extra = {"clock": clock} if clock is not None else {}
        self.voice = VoiceMcpTokenService(
            signing_key=VOICE_KEY, store=self.store, digest_key=DIGEST_KEY, resource_uris=URIS, scope_of=SCOPE_OF,
            user_repo=self.user_repo, whitelist_repo=self.whitelist_repo, alert_sink=self.alerts, **extra)
        self.oauth = MCPAuthorizationService(
            MagicMock(), jwt_secret=OAUTH_SECRET, mcp_resource_uri=FACTS_URI, consent_base_url=f"{BASE}/mcp/consent",
            allowed_redirect_hosts=("claude.ai",), drive_resource_uri=DRIVE_URI, scope_of=SCOPE_OF,
            user_repo=self.user_repo, whitelist_repo=self.whitelist_repo)

    @property
    def docs(self) -> Dict[str, Any]:
        return self.db.collection(COLLECTION).docs

    def oauth_token(self, resource: MCPResource, user_id: str = USER) -> str:
        """A real OAuth access token for `resource`, as the code and refresh flows mint it."""
        return self.oauth._mint_access_token(user_id=user_id, account_id=ACCOUNT, client_id="client-1",
                                             scopes=[SCOPE_OF[resource]], resource=URIS[resource], grant_id="g-1")


def claims(token: str) -> Dict[str, Any]:
    """A token's payload, unverified (the tests read what was minted)."""
    return jwt.decode(token, options={"verify_signature": False})


def resign(token: str, key: str, *, header: Optional[Dict[str, Any]] = None, **changes: Any) -> str:
    """`token`'s payload with `changes` (a None value drops the claim), signed HS256 under `key` with `header`
    (default: the original header)."""
    payload = {**claims(token), **changes}
    payload = {k: v for k, v in payload.items() if v is not None}
    head = header if header is not None else jwt.get_unverified_header(token)
    return jwt.encode(payload, key, algorithm="HS256", headers={k: v for k, v in head.items() if k not in ("alg", "typ")})
