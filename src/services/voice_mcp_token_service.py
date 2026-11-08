"""
VoiceMcpTokenService — the voice MCP access token kind (LELIK_VOICE_MCP_RFC §5): mint, verify, revoke.

Public interface:
    VoiceMcpTokens                      the two tokens of one call (facts, drive); repr hides them
    VerifiedVoiceToken                  what a verified voice token carries; repr hides the token
    is_voice_token(token) -> bool       the routing rule: the unverified header's `kid` is `voice-mcp`
    VoiceMcpTokenService
        mint(ticket, user_id, account_id) -> Optional[VoiceMcpTokens]
        verify(token, resource) -> Optional[VerifiedVoiceToken]
        revoke(ticket) -> None

Mint (§5.2, O1): at ticket redemption only. The whitelist is re-read as the OAuth refresh path reads it (the user
exists, has an email and the email is allowed); a refused user gets no tokens, a warning and an alert, never an error.
Then the live-call record `voice_mcp_call:{cid}` → `{user_id}` is written with the token's TTL, and two HS256 JWTs are
encoded in memory under the dedicated key, one per resource; nothing stores a token. A store or repository error
propagates (the caller fails the session-config: fail closed).

Verify (§5.3): the key, `kid`, `type`, `aud` (`voice:` + this resource), `exp`, the claims' shapes, then the live-call
record, whose `user_id` must equal `sub`. Any failure is None and one log line with the reason only; a store error is
a refusal too (logged by type). The OAuth verifier never sees a voice token and this one never an OAuth token: the
callers route on `is_voice_token`.

Logging (§5.4): no line carries a token, a ticket or an `Authorization` header; the call is named by `fingerprint(cid)`,
the user by the first 8 characters of the id, and every line carries `client=voice`.
"""
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import jwt

from ..domain.mcp_resource import MCPResource
from ..domain.secret_digest import fingerprint
from ..domain.voice_mcp_token import (
    VOICE_ALLOWED_TOOLS,
    VOICE_MCP_KID,
    VOICE_MCP_TOKEN_TTL_S,
    VOICE_MCP_TOKEN_TYPE,
    voice_audience,
    voice_call_id,
    voice_call_key,
)
from ..ports.alert_sink import AlertSinkPort
from ..ports.ephemeral_store import EphemeralStore
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..utils.logger import logger

_JWT_ALG = "HS256"
SIGNING_KEY_MIN_CHARS = 32
_REQUIRED_CLAIMS = ["exp", "iat", "sub", "aud"]


@dataclass(frozen=True)
class VoiceMcpTokens:
    """The two bearer tokens of one call. Never stored, never logged: the repr shows no token."""

    facts: str = field(repr=False)
    drive: str = field(repr=False)

    def to_wire(self) -> Dict[str, str]:
        """`{"facts": <token>, "drive": <token>}`: the session-config response field's value."""
        return {MCPResource.FACTS.value: self.facts, MCPResource.DRIVE.value: self.drive}


@dataclass(frozen=True)
class VerifiedVoiceToken:
    token: str = field(repr=False)
    user_id: str
    account_id: str
    scopes: List[str]
    resource: str
    expires_at: int
    tools: List[str]
    cid: str


def is_voice_token(token: str) -> bool:
    """True when the token's unverified header names `kid` `voice-mcp`; a token whose header does not parse is not."""
    try:
        header = jwt.get_unverified_header(token)
    except jwt.InvalidTokenError:
        return False
    return header.get("kid") == VOICE_MCP_KID


class VoiceMcpTokenService:
    def __init__(
        self,
        *,
        signing_key: str,
        store: EphemeralStore,
        digest_key: bytes,
        resource_uris: Mapping[MCPResource, str],
        scope_of: Mapping[MCPResource, str],
        user_repo: UserRepository,
        whitelist_repo: WhitelistRepository,
        alert_sink: Optional[AlertSinkPort],
        ttl_s: int = VOICE_MCP_TOKEN_TTL_S,
        clock: Callable[[], float] = time.time,
    ) -> None:
        """`signing_key`: `VOICE_MCP_SIGNING_KEY`, at least 32 characters, used for nothing else (O8); raises
        ValueError when shorter. `resource_uris` / `scope_of`: each resource's URI and its one scope (the scope strings
        live in composition). `store`: the voice tickets store the live-call record goes to. `alert_sink`: told of a
        whitelist refusal at mint; None logs only."""
        if len(signing_key or "") < SIGNING_KEY_MIN_CHARS:
            raise ValueError(f"the voice MCP signing key must be at least {SIGNING_KEY_MIN_CHARS} characters")
        self._key = signing_key
        self._store = store
        self._digest_key = digest_key
        self._audiences = {r: voice_audience(resource_uris[r]) for r in MCPResource}
        self._scope_of = {r: scope_of[r] for r in MCPResource}
        self._user_repo = user_repo
        self._whitelist_repo = whitelist_repo
        self._alerts = alert_sink
        self._ttl_s = ttl_s
        self._clock = clock

    # -----------------------------------------------------------------
    # Mint (§5.2)
    # -----------------------------------------------------------------

    async def mint(self, ticket: str, user_id: str, account_id: str) -> Optional[VoiceMcpTokens]:
        """The call's two tokens, or None when the user is no longer allowed (warned and alerted, not raised). Writes
        the live-call record first: a token never exists without it. Store and repository errors propagate."""
        cid = voice_call_id(self._digest_key, ticket)
        reason = await self._allow_reason(user_id)
        if reason is not None:
            logger.warning(f"[VoiceMCP] tokens refused: call={fingerprint(cid)} user={user_id[:8]} reason={reason} "
                           f"client=voice")
            await self._alert(f"Voice MCP: no memory or drive tools for call {fingerprint(cid)} "
                              f"(user {user_id[:8]}): {reason}")
            return None
        await self._store.set(voice_call_key(cid), {"user_id": user_id}, ttl_s=self._ttl_s)
        now = int(self._clock())
        tokens = VoiceMcpTokens(
            facts=self._encode(MCPResource.FACTS, user_id, account_id, cid, now),
            drive=self._encode(MCPResource.DRIVE, user_id, account_id, cid, now),
        )
        logger.info(f"[VoiceMCP] tokens minted: call={fingerprint(cid)} user={user_id[:8]} ttl={self._ttl_s} "
                    f"client=voice")
        return tokens

    # -----------------------------------------------------------------
    # Verify (§5.3)
    # -----------------------------------------------------------------

    async def verify(self, token: str, resource: MCPResource) -> Optional[VerifiedVoiceToken]:
        """The verified token for `resource`, or None (one log line with the reason only)."""
        decoded = self._decode(token, resource)
        if isinstance(decoded, str):
            self._log_refusal(resource, decoded)
            return None
        payload = decoded
        cid, user_id = payload["cid"], payload["sub"]
        try:
            record = await self._store.get(voice_call_key(cid))
        except Exception as exc:
            logger.error(f"[VoiceMCP] token refused: resource={resource.value} reason=store_error "
                         f"error={type(exc).__name__} client=voice")
            return None
        if record is None:
            self._log_refusal(resource, "call_ended", cid)
            return None
        if record.get("user_id") != user_id:
            self._log_refusal(resource, "other_user", cid)
            return None
        return VerifiedVoiceToken(
            token=token,
            user_id=user_id,
            account_id=payload["account_id"],
            scopes=payload["scope"].split(),
            resource=payload["aud"],
            expires_at=int(payload["exp"]),
            tools=list(payload["tools"]),
            cid=cid,
        )

    # -----------------------------------------------------------------
    # Revoke (§5.3)
    # -----------------------------------------------------------------

    async def revoke(self, ticket: str) -> None:
        """Delete the call's live-call record: every token of the call stops verifying. Idempotent."""
        cid = voice_call_id(self._digest_key, ticket)
        await self._store.delete(voice_call_key(cid))
        logger.info(f"[VoiceMCP] call record deleted: call={fingerprint(cid)} client=voice")

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _encode(self, resource: MCPResource, user_id: str, account_id: str, cid: str, now: int) -> str:
        payload: Dict[str, Any] = {
            "sub": user_id,
            "account_id": account_id,
            "type": VOICE_MCP_TOKEN_TYPE,
            "aud": self._audiences[resource],
            "scope": self._scope_of[resource],
            "tools": list(VOICE_ALLOWED_TOOLS[resource]),
            "cid": cid,
            "iat": now,
            "exp": now + self._ttl_s,
        }
        return jwt.encode(payload, self._key, algorithm=_JWT_ALG, headers={"kid": VOICE_MCP_KID})

    def _decode(self, token: str, resource: MCPResource) -> "Dict[str, Any] | str":
        """The verified payload, or the refusal reason."""
        try:
            if jwt.get_unverified_header(token).get("kid") != VOICE_MCP_KID:
                return "kid"
            payload = jwt.decode(token, self._key, algorithms=[_JWT_ALG], audience=self._audiences[resource],
                                 options={"require": _REQUIRED_CLAIMS})
        except jwt.ExpiredSignatureError:
            return "expired"
        except jwt.InvalidAudienceError:
            return "audience"
        except jwt.InvalidSignatureError:
            return "signature"
        except jwt.InvalidTokenError:
            return "malformed"
        if payload.get("type") != VOICE_MCP_TOKEN_TYPE:
            return "type"
        if not _claims_well_formed(payload):
            return "claims"
        return payload

    @staticmethod
    def _log_refusal(resource: MCPResource, reason: str, cid: Optional[str] = None) -> None:
        call = f" call={fingerprint(cid)}" if cid else ""
        logger.info(f"[VoiceMCP] token refused: resource={resource.value}{call} reason={reason} client=voice")

    async def _allow_reason(self, user_id: str) -> Optional[str]:
        """None when the user exists, has an email and is whitelisted; otherwise the refusal reason (the OAuth refresh
        path's rule, `MCPAuthorizationService._allow_reason`)."""
        user = await self._user_repo.get_user(user_id)
        if user is None:
            return "no_user"
        if not user.email:
            return "no_email"
        if not (await self._whitelist_repo.get_whitelist()).is_allowed(user.email):
            return "not_whitelisted"
        return None

    async def _alert(self, text: str) -> None:
        if self._alerts is None:
            return
        try:
            await self._alerts.post(text)
        except Exception as exc:
            logger.error(f"[VoiceMCP] alert failed: {type(exc).__name__} client=voice")


# `aud` too: PyJWT accepts a list audience that contains ours; a voice token names exactly one.
_STRING_CLAIMS: Tuple[str, ...] = ("sub", "account_id", "scope", "cid", "aud")


def _claims_well_formed(payload: Dict[str, Any]) -> bool:
    """Every string claim a non-empty string, and `tools` a list of strings."""
    if not all(isinstance(payload.get(k), str) and payload[k] for k in _STRING_CLAIMS):
        return False
    tools = payload.get("tools")
    return isinstance(tools, list) and all(isinstance(t, str) for t in tools)
