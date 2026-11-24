"""
MCPAuthorizationService — all OAuth 2.1 business logic for the remote MCP
server.

Owns:
- DCR client registration and redirect-URI allowlisting
- Consent-state JWT (serialized into the redirect to the consent page and
  verified when the user approves)
- Authorization code issuance + PKCE verification
- Access token (stateless JWT) and refresh token (stored by hash) lifecycle
- Refresh token rotation on every exchange
- The task queue's worker resource (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth"): served only when `scope_of` holds its
  scope (its URI is the facts host with `TASKS_PATH`, as the drive URI); a grant of it is bound to one worker
  (`worker_id` on the code and the refresh token, `wid` in the access token) and needs the user's flag at consent,
  at code exchange and at every refresh rotation

This service is intentionally SDK-free. The `mcp` Python SDK calls into
`MCPSdkOAuthProvider` (an adapter), which translates the SDK's typed
arguments into plain call-sites here.
"""

import hashlib
import secrets
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional, Tuple

import jwt

from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken, normalize_client_name
from ..domain.mcp_worker_grant import WorkerGrant
from ..domain.mcp_resource import (
    MCPResource,
    ResourceRefused,
    ScopeRefused,
    bind_resource,
    canonical_resource,
    tasks_resource_uri,
)
from ..ports.mcp_client_repository import MCPClientRepository
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..utils.logger import logger


# ---------------------------------------------------------------------------
# Service I/O types (SDK-neutral — translation lives in the adapter)
# ---------------------------------------------------------------------------


@dataclass
class ConsentRequest:
    """Decoded consent-state JWT. Opaque to the outside world."""
    client_id: str
    redirect_uri: str
    code_challenge: str
    code_challenge_method: str
    resource: Optional[str]
    scopes: List[str]
    state: Optional[str]


@dataclass
class AuthorizationParams:
    """Input to build_consent_redirect — mirrors SDK's AuthorizationParams."""
    redirect_uri: str
    code_challenge: str
    scopes: List[str]
    state: Optional[str] = None
    resource: Optional[str] = None
    code_challenge_method: str = "S256"


@dataclass
class IssuedToken:
    access_token: str
    refresh_token: Optional[str]
    expires_in: int
    scopes: List[str]
    resource: Optional[str]
    token_type: str = "Bearer"


@dataclass
class VerifiedAccessToken:
    token: str
    client_id: str
    user_id: str
    account_id: str
    scopes: List[str]
    resource: Optional[str]
    expires_at: int  # unix seconds
    grant_id: Optional[str] = None  # the `gid` claim; None on a token minted before the grant id existed
    worker_id: Optional[str] = None  # the `wid` claim: a tasks-resource grant's worker; None on every other grant


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MCPAuthError(Exception):
    """Raised on any OAuth validation failure."""


class MCPInvalidClient(MCPAuthError):
    pass


class MCPInvalidGrant(MCPAuthError):
    pass


class MCPInvalidRedirectURI(MCPAuthError):
    pass


class MCPInvalidResource(MCPAuthError):
    pass


class MCPInvalidScope(MCPAuthError):
    pass


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


_ACCESS_TOKEN_TYPE = "mcp_access"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_CONSENT_AUD = "mcp-consent"
_JWT_ALG = "HS256"


def _issued(token: MCPRefreshToken) -> datetime:
    return token.issued_at or _EPOCH


class MCPAuthorizationService:
    def __init__(
        self,
        repo: MCPClientRepository,
        *,
        jwt_secret: str,
        mcp_resource_uri: str,
        consent_base_url: str,
        allowed_redirect_hosts: Tuple[str, ...],
        drive_resource_uri: str,
        scope_of: Mapping[MCPResource, str],
        user_repo: UserRepository,
        whitelist_repo: WhitelistRepository,
        access_token_ttl: int = 3600,
        refresh_token_ttl: int = 2592000,
        auth_code_ttl: int = 600,
        consent_request_ttl: int = 600,
    ):
        """
        Pure DI — no config imports. The composition layer reads values
        off AuthConfig and passes them here as plain args so this module
        stays within the services-layer import budget.

        Args:
            repo: storage port
            jwt_secret: HS256 signing secret (min 32 chars)
            mcp_resource_uri: canonical resource URI (aud for access
                tokens; used to derive the RFC 8707 resource indicator)
            consent_base_url: absolute URL of the consent page (e.g.
                "https://dev.alekbot.app/mcp/consent")
            allowed_redirect_hosts: tuple of allowed redirect-URI host
                substrings (e.g. ("claude.ai", "claude.com", "localhost"))
            drive_resource_uri: canonical URI of the drive connector
                resource (the second audience this server mints)
            scope_of: the one OAuth scope of each resource (injected: the strings live in composition). The tasks
                resource is served only when it has a scope here; without one its scope is refused (fail closed).
                Its URI is derived from `mcp_resource_uri`, as composition derives the drive URI (no new key)
            user_repo / whitelist_repo: read at every refresh rotation
                (required, no default: a missing reader must fail the
                build, not open the gate)
            access_token_ttl / refresh_token_ttl / auth_code_ttl /
            consent_request_ttl: seconds
        """
        self._repo = repo
        self._secret = jwt_secret
        self._resource_uri = mcp_resource_uri
        self._uris = {
            MCPResource.FACTS: canonical_resource(mcp_resource_uri),
            MCPResource.DRIVE: canonical_resource(drive_resource_uri),
        }
        self._scope_of = dict(scope_of)
        self._tasks_uri: Optional[str] = None
        if MCPResource.TASKS in self._scope_of:
            self._tasks_uri = canonical_resource(tasks_resource_uri(mcp_resource_uri))
            self._uris[MCPResource.TASKS] = self._tasks_uri
        self._user_repo = user_repo
        self._whitelist_repo = whitelist_repo
        self._consent_base_url = consent_base_url
        self._allowed_hosts = tuple(allowed_redirect_hosts)
        self._access_token_ttl = access_token_ttl
        self._refresh_token_ttl = refresh_token_ttl
        self._auth_code_ttl = auth_code_ttl
        self._consent_request_ttl = consent_request_ttl
        if len(self._secret) < 32:
            raise ValueError("jwt_secret must be at least 32 characters")

    # -----------------------------------------------------------------
    # Clients (DCR is handled by the SDK; we just persist and allowlist)
    # -----------------------------------------------------------------

    async def save_client(self, client: MCPClient) -> None:
        """
        Persist a DCR-registered client. The SDK's registration handler
        generates client_id + client_secret and hands them to us via the
        adapter. We enforce the redirect-URI host allowlist here — any
        violation raises MCPInvalidRedirectURI and the registration is
        rejected.
        """
        if not client.redirect_uris:
            raise MCPInvalidRedirectURI("redirect_uris is required")
        for uri in client.redirect_uris:
            self._validate_redirect_host(uri)
        client = client.model_copy(update={"client_name": normalize_client_name(client.client_name)})
        await self._repo.save_client(client)
        logger.info(
            f"🆕 MCP client registered: {client.client_id} "
            f"({client.client_name or 'unnamed'})"
        )

    async def get_client(self, client_id: str) -> Optional[MCPClient]:
        return await self._repo.get_client(client_id)

    # -----------------------------------------------------------------
    # /authorize — consent redirect
    # -----------------------------------------------------------------

    async def build_consent_redirect(
        self, client: MCPClient, params: AuthorizationParams
    ) -> str:
        if params.redirect_uri not in client.redirect_uris:
            raise MCPInvalidRedirectURI(
                f"redirect_uri not registered for client {client.client_id}"
            )
        if not params.code_challenge:
            raise MCPAuthError("code_challenge is required (PKCE mandatory)")
        if params.code_challenge_method != "S256":
            raise MCPAuthError("only S256 code_challenge_method is supported")

        try:
            _, uri, scopes = bind_resource(params.resource, params.scopes, self._uris, self._scope_of)
        except ResourceRefused as e:
            raise MCPInvalidResource(str(e)) from e
        except ScopeRefused as e:
            raise MCPInvalidScope(str(e)) from e
        # The SDK checks requested scopes against the client's registered scope only when a scope was sent; a
        # resource with no scope is bound to the resource's own scope here, so it is checked here too. A client
        # stored without a scope is a pre-DCR-change one, which could only ever hold the facts scope.
        registered = client.scope.split() or [self._scope_of[MCPResource.FACTS]]
        if any(sc not in registered for sc in scopes):
            raise MCPInvalidScope("scope not registered for this client")
        params = replace(params, resource=uri, scopes=scopes)

        consent_jwt = self._mint_consent_jwt(client.client_id, params)
        # The consent page URL is a simple query-string — the caller
        # (SDK handler) 302-redirects the browser here.
        return f"{self._consent_base_url}?req={consent_jwt}"

    def verify_consent_request(self, consent_jwt: str) -> ConsentRequest:
        try:
            payload = jwt.decode(
                consent_jwt,
                self._secret,
                algorithms=[_JWT_ALG],
                audience=_CONSENT_AUD,
            )
        except jwt.InvalidTokenError as e:
            raise MCPAuthError(f"invalid consent request: {e}") from e

        return ConsentRequest(
            client_id=payload["client_id"],
            redirect_uri=payload["redirect_uri"],
            code_challenge=payload["code_challenge"],
            code_challenge_method=payload.get("code_challenge_method", "S256"),
            resource=payload.get("resource"),
            scopes=list(payload.get("scopes", [])),
            state=payload.get("state"),
        )

    async def issue_auth_code_for_consent(
        self, consent_jwt: str, user_id: str, account_id: str, worker_id: Optional[str] = None
    ) -> tuple[str, ConsentRequest]:
        """
        Verify the consent JWT, mint a one-shot authorization code bound
        to (user_id, account_id), persist it, and return (code, req).
        The caller uses `req.redirect_uri` + `req.state` to build the
        redirect back to the OAuth client.

        A grant of the tasks resource needs `worker_id` (the worker the
        user picked; the caller has checked it is the user's active one)
        and a user allowed it now (lock, whitelist, flag); any other grant
        refuses a `worker_id`. A refusal raises MCPAuthError, no code.
        """
        req = self.verify_consent_request(consent_jwt)
        if self._is_tasks(req.resource):
            if not worker_id:
                raise MCPAuthError("a worker is required for the task queue resource")
            reason = await self._allow_reason(user_id, req.resource)
            if reason is not None:
                logger.warning(f"MCP consent refused: user={user_id[:8]} resource=tasks reason={reason}")
                raise MCPAuthError("grant not allowed")
        elif worker_id is not None:
            raise MCPAuthError("a worker is only bound to a grant of the task queue resource")
        code_value = secrets.token_urlsafe(32)
        expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=self._auth_code_ttl
        )
        auth_code = MCPAuthCode(
            code=code_value,
            client_id=req.client_id,
            user_id=user_id,
            account_id=account_id,
            redirect_uri=req.redirect_uri,
            code_challenge=req.code_challenge,
            code_challenge_method=req.code_challenge_method,
            resource=req.resource,
            scopes=req.scopes,
            expires_at=expires_at,
            worker_id=worker_id,
        )
        await self._repo.save_auth_code(auth_code)
        logger.info(
            f"🎟️  MCP auth code issued for client={req.client_id} "
            f"user={user_id[:8]}"
        )
        return code_value, req

    async def load_auth_code(self, code: str) -> Optional[MCPAuthCode]:
        """Non-consuming load used by SDK's `load_authorization_code` hook."""
        return await self._repo.get_auth_code(code)

    # -----------------------------------------------------------------
    # /token — authorization_code grant
    # -----------------------------------------------------------------
    #
    # The `mcp` SDK's token handler validates client_id match, code
    # expiry, redirect_uri equality, and PKCE BEFORE calling us. By the
    # time we get here the code is known-good; our only jobs are:
    #   1. Consume the code atomically (one-shot enforcement)
    #   2. Mint a fresh access + refresh token pair

    async def finalize_auth_code(self, code: str) -> IssuedToken:
        code_obj = await self._repo.consume_auth_code(code)
        if code_obj is None:
            raise MCPInvalidGrant("authorization code not found or already used")
        if await self._user_inactive(code_obj.user_id):
            # A code issued from a Cabinet session just before the account lock: consumed, never exchanged.
            logger.warning(f"MCP code exchange refused: user={code_obj.user_id[:8]} reason=inactive")
            raise MCPInvalidGrant("grant no longer allowed")
        if self._is_tasks(code_obj.resource):
            # The tasks resource re-checks the whole allow rule (flag included) at exchange; the code is consumed.
            reason = "no_worker" if not code_obj.worker_id else await self._allow_reason(
                code_obj.user_id, code_obj.resource)
            if reason is not None:
                logger.warning(f"MCP code exchange refused: user={code_obj.user_id[:8]} resource=tasks "
                               f"reason={reason}")
                raise MCPInvalidGrant("grant no longer allowed")

        now = datetime.now(timezone.utc)
        grant_id = self._new_grant_id()
        access_token = self._mint_access_token(
            user_id=code_obj.user_id,
            account_id=code_obj.account_id,
            client_id=code_obj.client_id,
            scopes=code_obj.scopes,
            resource=code_obj.resource,
            grant_id=grant_id,
            worker_id=code_obj.worker_id,
        )
        refresh_token_value = secrets.token_urlsafe(48)
        await self._repo.save_refresh_token(
            MCPRefreshToken(
                token_hash=self._hash_token(refresh_token_value),
                client_id=code_obj.client_id,
                user_id=code_obj.user_id,
                account_id=code_obj.account_id,
                scopes=code_obj.scopes,
                resource=code_obj.resource,
                expires_at=now + timedelta(seconds=self._refresh_token_ttl),
                grant_id=grant_id,
                worker_id=code_obj.worker_id,
                granted_at=now,
                issued_at=now,
            )
        )
        return IssuedToken(
            access_token=access_token,
            refresh_token=refresh_token_value,
            expires_in=self._access_token_ttl,
            scopes=code_obj.scopes,
            resource=code_obj.resource,
        )

    # -----------------------------------------------------------------
    # /token — refresh_token grant
    # -----------------------------------------------------------------
    #
    # Same contract: the SDK pre-validates (client_id match, expiry,
    # scope downscoping). We rotate (revoke-old + issue-new) and mint.

    async def load_refresh_token(
        self, refresh_token_value: str
    ) -> Optional[MCPRefreshToken]:
        """Non-consuming load for SDK's load_refresh_token hook."""
        token = await self._repo.get_refresh_token(self._hash_token(refresh_token_value))
        if token is None or not token.is_active:
            return None
        return token

    async def rotate_refresh_token(
        self,
        refresh_token_value: str,
        requested_scopes: Optional[List[str]] = None,
    ) -> IssuedToken:
        existing = await self.load_refresh_token(refresh_token_value)
        if existing is None:
            raise MCPInvalidGrant("refresh token not found or revoked")
        try:
            await self._require_allowed(existing.user_id, existing.resource)
        except MCPInvalidGrant:
            await self._repo.revoke_refresh_token(self._hash_token(refresh_token_value))
            raise

        final_scopes = list(requested_scopes) if requested_scopes else list(existing.scopes)
        try:
            bind_resource(existing.resource, final_scopes, self._uris, self._scope_of)
        except (ResourceRefused, ScopeRefused) as e:
            raise MCPInvalidGrant(f"refresh scope not allowed: {e}") from e
        now = datetime.now(timezone.utc)
        # A token stored before the grant id existed gets one at its first rotation and keeps it after.
        grant_id = existing.grant_id or self._new_grant_id()

        # Revoke first, then save: a save-first order would let a replayed old token mint again. A link fetch in
        # the gap between the two sees no active token for the grant and is refused (the client asks again).
        # The revoke is the one-exchange claim: a concurrent exchange of the same token that revoked it first wins.
        if not await self._repo.revoke_refresh_token(self._hash_token(refresh_token_value)):
            logger.warning(f"MCP refresh token replayed concurrently for user={existing.user_id[:8]}; refused")
            raise MCPInvalidGrant("refresh token not found or revoked")
        new_refresh = secrets.token_urlsafe(48)
        await self._repo.save_refresh_token(
            MCPRefreshToken(
                token_hash=self._hash_token(new_refresh),
                client_id=existing.client_id,
                user_id=existing.user_id,
                account_id=existing.account_id,
                scopes=final_scopes,
                resource=existing.resource,
                expires_at=now + timedelta(seconds=self._refresh_token_ttl),
                grant_id=grant_id,
                worker_id=existing.worker_id,
                granted_at=existing.granted_at,
                issued_at=now,
            )
        )
        access_token = self._mint_access_token(
            user_id=existing.user_id,
            account_id=existing.account_id,
            client_id=existing.client_id,
            scopes=final_scopes,
            resource=existing.resource,
            grant_id=grant_id,
            worker_id=existing.worker_id,
        )
        return IssuedToken(
            access_token=access_token,
            refresh_token=new_refresh,
            expires_in=self._access_token_ttl,
            scopes=final_scopes,
            resource=existing.resource,
        )

    # -----------------------------------------------------------------
    # Bearer verification (SDK calls this on every MCP request)
    # -----------------------------------------------------------------

    def verify_access_token(
        self, token_str: str, *, resource: Optional[str] = None
    ) -> Optional[VerifiedAccessToken]:
        try:
            # Audiences are compared in canonical form: tokens minted with the raw configured URI (legacy) and
            # with the canonical one (bound at authorize) both verify against the same resource.
            payload = jwt.decode(
                token_str,
                self._secret,
                algorithms=[_JWT_ALG],
                options={"verify_aud": False},
            )
        except jwt.InvalidTokenError as e:
            logger.debug(f"MCP access token rejected: {e}")
            return None

        aud = payload.get("aud")
        if not isinstance(aud, str) or canonical_resource(aud) != canonical_resource(
            resource or self._resource_uri
        ):
            logger.debug("MCP access token rejected: audience does not match")
            return None

        if payload.get("type") != _ACCESS_TOKEN_TYPE:
            logger.debug("MCP access token rejected: wrong type")
            return None

        return VerifiedAccessToken(
            token=token_str,
            client_id=payload["client_id"],
            user_id=payload["sub"],
            account_id=payload["account_id"],
            scopes=list(payload.get("scope", "").split()) if payload.get("scope") else [],
            resource=payload.get("aud"),
            expires_at=int(payload["exp"]),
            grant_id=payload.get("gid"),
            worker_id=payload.get("wid"),
        )

    async def revoke_refresh_token_value(self, refresh_token_value: str) -> None:
        """RFC 7009 token revocation for refresh tokens."""
        await self._repo.revoke_refresh_token(self._hash_token(refresh_token_value))

    # -----------------------------------------------------------------
    # Grant check (the one-time drive link re-checks it at fetch)
    # -----------------------------------------------------------------

    async def grant_allowed(self, user_id: str, grant_id: Optional[str]) -> bool:
        """True when the grant still has an active refresh token and its user is still whitelisted.

        No grant id (a link issued from a token minted before the grant id existed) → False. A repository
        error propagates; the caller fails closed. The refusal log carries the reason, never the email.
        """
        reason: Optional[str]
        if not grant_id:
            reason = "no_grant"
        elif not await self._repo.has_active_grant(user_id, grant_id, datetime.now(timezone.utc)):
            reason = "grant_inactive"
        else:
            reason = await self._allow_reason(user_id)
        if reason is not None:
            logger.warning(f"MCP link grant refused: user={user_id[:8]} reason={reason}")
            return False
        return True

    # -----------------------------------------------------------------
    # The task queue's grants, per worker (the Cabinet, EXTERNAL_TASK_QUEUE_RFC §5.6)
    # -----------------------------------------------------------------

    async def worker_grants(self, user_id: str, worker_id: str) -> List[WorkerGrant]:
        """The worker's live OAuth grants (an unrevoked, unexpired refresh token), oldest grant first. The client name
        is the registered one (its client id when the client is gone). Does not check that the worker is the user's:
        the caller does."""
        latest: Dict[str, MCPRefreshToken] = {}
        for token in await self._repo.list_worker_grants(user_id, worker_id, datetime.now(timezone.utc)):
            if not token.grant_id:
                continue
            held = latest.get(token.grant_id)
            if held is None or _issued(token) > _issued(held):
                latest[token.grant_id] = token
        names: Dict[str, str] = {}
        grants = []
        for grant_id, token in latest.items():
            if token.client_id not in names:
                client = await self._repo.get_client(token.client_id)
                names[token.client_id] = client.client_name if client is not None else token.client_id
            grants.append(WorkerGrant(grant_id=grant_id, client_id=token.client_id,
                                      client_name=names[token.client_id], granted_at=token.granted_at,
                                      last_refresh=token.issued_at))
        return sorted(grants, key=lambda g: (g.granted_at or _EPOCH, g.grant_id))

    async def revoke_grant(self, user_id: str, grant_id: str) -> int:
        """Revoke every refresh token of the user's grant; the grant's live access tokens are then refused on their
        next tasks call (`grant_allowed`). Returns how many tokens it revoked."""
        return await self._repo.revoke_grant(user_id, grant_id, datetime.now(timezone.utc))

    async def revoke_worker_grants(self, user_id: str, worker_id: str) -> int:
        """Revoke every grant bound to the worker (its removal, §5.6). Returns how many tokens it revoked."""
        return await self._repo.revoke_worker_grants(user_id, worker_id, datetime.now(timezone.utc))

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    async def _user_inactive(self, user_id: str) -> bool:
        """Whether the stored user exists and is inactive (the account lock, CABINET_DEVICE_SESSIONS_RFC §7.8), read
        fresh. A read error propagates (no token is minted)."""
        user = await self._user_repo.get_user(user_id)
        return user is not None and not user.is_active

    def _is_tasks(self, resource: Optional[str]) -> bool:
        """The resource is the task queue's worker resource, and this server serves it."""
        return (self._tasks_uri is not None and resource is not None
                and canonical_resource(resource) == self._tasks_uri)

    async def _allow_reason(self, user_id: str, resource: Optional[str] = None) -> Optional[str]:
        """None when the user exists, is active (not locked: CABINET_DEVICE_SESSIONS_RFC §7.8), has an email and is
        whitelisted, and, for the task queue's resource, has the queue's flag (EXTERNAL_TASK_QUEUE_RFC §5.4);
        otherwise the refusal reason."""
        user = await self._user_repo.get_user(user_id)
        if user is None:
            return "no_user"
        if not user.is_active:
            return "inactive"
        if not user.email:
            return "no_email"
        if not (await self._whitelist_repo.get_whitelist()).is_allowed(user.email):
            return "not_whitelisted"
        if self._is_tasks(resource) and not user.config.external_tasks_enabled:
            return "not_enabled"
        return None

    async def _require_allowed(self, user_id: str, resource: Optional[str] = None) -> None:
        """Raise MCPInvalidGrant unless the grant's user exists, has an email and is whitelisted (and, for the task
        queue's resource, has its flag).

        A repository error propagates (the SDK answers 500, no token is minted: fail closed). The log line
        carries the reason, never the email.
        """
        reason = await self._allow_reason(user_id, resource)
        if reason is not None:
            logger.warning(f"MCP refresh refused: user={user_id[:8]} reason={reason}")
            raise MCPInvalidGrant("grant no longer allowed")

    @staticmethod
    def _new_grant_id() -> str:
        return secrets.token_urlsafe(16)

    def _validate_redirect_host(self, uri: str) -> None:
        from urllib.parse import urlparse

        parsed = urlparse(uri)
        if parsed.scheme not in ("https", "http"):
            raise MCPInvalidRedirectURI(f"invalid redirect scheme: {parsed.scheme}")
        if parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1"):
            raise MCPInvalidRedirectURI("http only allowed on localhost")
        host = parsed.hostname or ""
        if not any(host == h or host.endswith("." + h) for h in self._allowed_hosts):
            raise MCPInvalidRedirectURI(f"redirect host not allowed: {host}")

    def _mint_consent_jwt(self, client_id: str, params: AuthorizationParams) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self._consent_request_ttl)
        payload: Dict[str, Any] = {
            "client_id": client_id,
            "redirect_uri": params.redirect_uri,
            "code_challenge": params.code_challenge,
            "code_challenge_method": params.code_challenge_method,
            "resource": params.resource,
            "scopes": list(params.scopes),
            "state": params.state,
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "aud": _CONSENT_AUD,
        }
        return jwt.encode(payload, self._secret, algorithm=_JWT_ALG)

    def _mint_access_token(
        self,
        *,
        user_id: str,
        account_id: str,
        client_id: str,
        scopes: List[str],
        resource: Optional[str],
        grant_id: Optional[str] = None,
        worker_id: Optional[str] = None,
    ) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self._access_token_ttl)
        payload: Dict[str, Any] = {
            "sub": user_id,
            "account_id": account_id,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "type": _ACCESS_TOKEN_TYPE,
            "aud": resource or self._resource_uri,
        }
        if grant_id:
            payload["gid"] = grant_id
        if worker_id:
            payload["wid"] = worker_id
        return jwt.encode(payload, self._secret, algorithm=_JWT_ALG)

    @staticmethod
    def _hash_token(token_plain: str) -> str:
        return hashlib.sha256(token_plain.encode("utf-8")).hexdigest()
