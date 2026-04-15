"""
MCPSdkOAuthProvider — thin shim between the `mcp` Python SDK's
OAuthAuthorizationServerProvider protocol and our SDK-free
MCPAuthorizationService.

This module lives in `src/composition/` rather than `src/adapters/`
because it depends on a service (`MCPAuthorizationService`). The
hexagonal isolation rule (REQ-ARCH-01) forbids adapters/ → services/.
Composition is the only layer allowed to cross all boundaries — it
wires the SDK's OAuth protocol to the service's business logic.

Contents: the SDK's `OAuthAuthorizationServerProvider` protocol has
nine methods; each one translates SDK types into domain types, calls
into the service, and translates the result back. Zero business logic.

The SDK expects custom subclasses of AuthorizationCode, RefreshToken,
AccessToken so we can carry user_id + account_id through the auth flow.
These subclasses live at the top of this file — they're SDK-shaped data
carriers, not domain models.
"""

from datetime import datetime, timezone
from typing import List, Optional

from mcp.server.auth.provider import (
    AccessToken as SDKAccessToken,
    AuthorizationCode as SDKAuthorizationCode,
    AuthorizationParams as SDKAuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken as SDKRefreshToken,
    RegistrationError,
    RegistrationErrorCode,
    TokenError,
    TokenErrorCode,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from pydantic import AnyUrl

from ..domain.mcp import MCPClient
from ..services.mcp_authorization_service import (
    AuthorizationParams,
    MCPAuthError,
    MCPAuthorizationService,
    MCPInvalidGrant,
    MCPInvalidRedirectURI,
)
from ..utils.logger import logger


# ---------------------------------------------------------------------------
# SDK-shaped subclasses that also carry user_id / account_id.
# The SDK's BearerAuthBackend surfaces AccessToken via
# `request.user.access_token`, so tool handlers can downcast and read the
# extra fields without touching contextvars at the SDK boundary.
# ---------------------------------------------------------------------------


class AlekAuthorizationCode(SDKAuthorizationCode):
    user_id: str
    account_id: str


class AlekRefreshToken(SDKRefreshToken):
    user_id: str
    account_id: str


class AlekAccessToken(SDKAccessToken):
    user_id: str
    account_id: str


# ---------------------------------------------------------------------------
# Translation helpers (pure functions, no I/O)
# ---------------------------------------------------------------------------


def _domain_to_sdk_client(c: MCPClient) -> OAuthClientInformationFull:
    """Translate our domain MCPClient to the SDK's client info model."""
    issued_at = int(c.created_at.timestamp())
    return OAuthClientInformationFull(
        client_id=c.client_id,
        client_secret=c.client_secret,
        client_secret_expires_at=c.client_secret_expires_at,
        client_id_issued_at=issued_at,
        redirect_uris=[AnyUrl(u) for u in c.redirect_uris],
        token_endpoint_auth_method=c.token_endpoint_auth_method,
        grant_types=list(c.grant_types),
        response_types=list(c.response_types),
        scope=c.scope,
        client_name=c.client_name,
    )


def _sdk_to_domain_client(info: OAuthClientInformationFull) -> MCPClient:
    """Translate SDK client info back to our domain model."""
    created_at_ts = info.client_id_issued_at or int(datetime.now(timezone.utc).timestamp())
    return MCPClient(
        client_id=info.client_id,
        client_secret=info.client_secret,
        client_secret_expires_at=info.client_secret_expires_at,
        client_name=info.client_name or "",
        redirect_uris=[str(u) for u in info.redirect_uris],
        grant_types=list(info.grant_types),
        response_types=list(info.response_types),
        scope=info.scope or "",
        token_endpoint_auth_method=info.token_endpoint_auth_method or "client_secret_post",
        created_at=datetime.fromtimestamp(created_at_ts, tz=timezone.utc),
    )


def _sdk_authorize_params_to_domain(
    p: SDKAuthorizationParams,
) -> AuthorizationParams:
    return AuthorizationParams(
        redirect_uri=str(p.redirect_uri),
        code_challenge=p.code_challenge,
        scopes=list(p.scopes or []),
        state=p.state,
        resource=p.resource,
        # PKCE method is not carried on SDK AuthorizationParams — S256 is
        # hardcoded in the SDK's /authorize handler and in our service.
        code_challenge_method="S256",
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class MCPSdkOAuthProvider(
    OAuthAuthorizationServerProvider[
        AlekAuthorizationCode, AlekRefreshToken, AlekAccessToken
    ]
):
    def __init__(self, service: MCPAuthorizationService):
        self._service = service

    # -----------------------------------------------------------------
    # Client (DCR + /token client auth lookup)
    # -----------------------------------------------------------------

    async def get_client(self, client_id: str) -> Optional[OAuthClientInformationFull]:
        domain = await self._service.get_client(client_id)
        if domain is None:
            return None
        return _domain_to_sdk_client(domain)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        try:
            await self._service.save_client(_sdk_to_domain_client(client_info))
        except MCPInvalidRedirectURI as e:
            logger.warning(f"DCR rejected: {e}")
            raise RegistrationError(
                error="invalid_redirect_uri",
                error_description=str(e),
            )
        except MCPAuthError as e:
            raise RegistrationError(
                error="invalid_client_metadata",
                error_description=str(e),
            )

    # -----------------------------------------------------------------
    # /authorize — redirect to our consent page
    # -----------------------------------------------------------------

    async def authorize(
        self,
        client: OAuthClientInformationFull,
        params: SDKAuthorizationParams,
    ) -> str:
        domain_client = _sdk_to_domain_client(client)
        try:
            return await self._service.build_consent_redirect(
                domain_client, _sdk_authorize_params_to_domain(params)
            )
        except MCPInvalidRedirectURI as e:
            raise MCPAuthError(f"redirect_uri not allowed: {e}") from e

    # -----------------------------------------------------------------
    # Authorization codes
    # -----------------------------------------------------------------

    async def load_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: str,
    ) -> Optional[AlekAuthorizationCode]:
        code_obj = await self._service.load_auth_code(authorization_code)
        if code_obj is None or code_obj.client_id != client.client_id:
            return None
        return AlekAuthorizationCode(
            code=code_obj.code,
            scopes=list(code_obj.scopes),
            expires_at=code_obj.expires_at.timestamp(),
            client_id=code_obj.client_id,
            code_challenge=code_obj.code_challenge,
            redirect_uri=AnyUrl(code_obj.redirect_uri),
            redirect_uri_provided_explicitly=True,
            resource=code_obj.resource,
            user_id=code_obj.user_id,
            account_id=code_obj.account_id,
        )

    async def exchange_authorization_code(
        self,
        client: OAuthClientInformationFull,
        authorization_code: AlekAuthorizationCode,
    ) -> OAuthToken:
        # SDK token handler has already verified PKCE, client_id match,
        # redirect_uri equality and expiry before calling us. Our job is
        # only to consume the code atomically and mint fresh tokens.
        try:
            issued = await self._service.finalize_auth_code(authorization_code.code)
        except MCPInvalidGrant as e:
            raise TokenError(error="invalid_grant", error_description=str(e))

        return OAuthToken(
            access_token=issued.access_token,
            token_type="Bearer",
            expires_in=issued.expires_in,
            scope=" ".join(issued.scopes) if issued.scopes else None,
            refresh_token=issued.refresh_token,
        )

    # -----------------------------------------------------------------
    # Refresh tokens
    # -----------------------------------------------------------------

    async def load_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: str,
    ) -> Optional[AlekRefreshToken]:
        domain_token = await self._service.load_refresh_token(refresh_token)
        if domain_token is None or domain_token.client_id != client.client_id:
            return None
        return AlekRefreshToken(
            token=refresh_token,
            client_id=domain_token.client_id,
            scopes=list(domain_token.scopes),
            expires_at=int(domain_token.expires_at.timestamp()),
            user_id=domain_token.user_id,
            account_id=domain_token.account_id,
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: AlekRefreshToken,
        scopes: List[str],
    ) -> OAuthToken:
        try:
            issued = await self._service.rotate_refresh_token(
                refresh_token_value=refresh_token.token,
                requested_scopes=scopes or None,
            )
        except MCPInvalidGrant as e:
            raise TokenError(error="invalid_grant", error_description=str(e))

        return OAuthToken(
            access_token=issued.access_token,
            token_type="Bearer",
            expires_in=issued.expires_in,
            scope=" ".join(issued.scopes) if issued.scopes else None,
            refresh_token=issued.refresh_token,
        )

    # -----------------------------------------------------------------
    # Bearer middleware (called on every authenticated MCP request)
    # -----------------------------------------------------------------

    async def load_access_token(self, token: str) -> Optional[AlekAccessToken]:
        verified = self._service.verify_access_token(token)
        if verified is None:
            return None
        return AlekAccessToken(
            token=verified.token,
            client_id=verified.client_id,
            scopes=list(verified.scopes),
            expires_at=verified.expires_at,
            resource=verified.resource,
            user_id=verified.user_id,
            account_id=verified.account_id,
        )

    async def revoke_token(self, token: AlekAccessToken | AlekRefreshToken) -> None:
        # Access tokens are stateless JWTs — nothing to revoke server-side
        # until we add a blacklist. Refresh tokens we mark revoked.
        if isinstance(token, AlekRefreshToken):
            await self._service.revoke_refresh_token_value(token.token)
