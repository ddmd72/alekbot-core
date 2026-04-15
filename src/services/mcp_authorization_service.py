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

This service is intentionally SDK-free. The `mcp` Python SDK calls into
`MCPSdkOAuthProvider` (an adapter), which translates the SDK's typed
arguments into plain call-sites here.
"""

import hashlib
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import jwt

from ..domain.mcp import MCPAuthCode, MCPClient, MCPRefreshToken
from ..ports.mcp_client_repository import MCPClientRepository
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


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


_ACCESS_TOKEN_TYPE = "mcp_access"
_CONSENT_AUD = "mcp-consent"
_JWT_ALG = "HS256"


class MCPAuthorizationService:
    def __init__(
        self,
        repo: MCPClientRepository,
        *,
        jwt_secret: str,
        mcp_resource_uri: str,
        consent_base_url: str,
        allowed_redirect_hosts: Tuple[str, ...],
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
            access_token_ttl / refresh_token_ttl / auth_code_ttl /
            consent_request_ttl: seconds
        """
        self._repo = repo
        self._secret = jwt_secret
        self._resource_uri = mcp_resource_uri
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
        self, consent_jwt: str, user_id: str, account_id: str
    ) -> tuple[str, ConsentRequest]:
        """
        Verify the consent JWT, mint a one-shot authorization code bound
        to (user_id, account_id), persist it, and return (code, req).
        The caller uses `req.redirect_uri` + `req.state` to build the
        redirect back to the OAuth client.
        """
        req = self.verify_consent_request(consent_jwt)
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

        now = datetime.now(timezone.utc)
        access_token = self._mint_access_token(
            user_id=code_obj.user_id,
            account_id=code_obj.account_id,
            client_id=code_obj.client_id,
            scopes=code_obj.scopes,
            resource=code_obj.resource,
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

        final_scopes = list(requested_scopes) if requested_scopes else list(existing.scopes)
        now = datetime.now(timezone.utc)

        await self._repo.revoke_refresh_token(self._hash_token(refresh_token_value))
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
            )
        )
        access_token = self._mint_access_token(
            user_id=existing.user_id,
            account_id=existing.account_id,
            client_id=existing.client_id,
            scopes=final_scopes,
            resource=existing.resource,
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

    def verify_access_token(self, token_str: str) -> Optional[VerifiedAccessToken]:
        try:
            payload = jwt.decode(
                token_str,
                self._secret,
                algorithms=[_JWT_ALG],
                audience=self._resource_uri,
            )
        except jwt.InvalidTokenError as e:
            logger.debug(f"MCP access token rejected: {e}")
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
        )

    async def revoke_refresh_token_value(self, refresh_token_value: str) -> None:
        """RFC 7009 token revocation for refresh tokens."""
        await self._repo.revoke_refresh_token(self._hash_token(refresh_token_value))

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

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
    ) -> str:
        now = datetime.now(timezone.utc)
        exp = now + timedelta(seconds=self._access_token_ttl)
        payload = {
            "sub": user_id,
            "account_id": account_id,
            "client_id": client_id,
            "scope": " ".join(scopes),
            "iat": int(now.timestamp()),
            "exp": int(exp.timestamp()),
            "type": _ACCESS_TOKEN_TYPE,
            "aud": resource or self._resource_uri,
        }
        return jwt.encode(payload, self._secret, algorithm=_JWT_ALG)

    @staticmethod
    def _hash_token(token_plain: str) -> str:
        return hashlib.sha256(token_plain.encode("utf-8")).hexdigest()
