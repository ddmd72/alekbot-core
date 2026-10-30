"""MicrosoftOAuthProvider — the OAuth edge of the Microsoft realm (MULTI_DRIVE_RFC §5.1, §5.2).

Consumers tenant, PKCE S256. The account identity is Graph `/me` (`id`, and `mail` or `userPrincipalName` as label).
Microsoft is never revoked at the provider (`RevocationScope.NONE`). Granted scopes are returned in the short form
(`Tasks.ReadWrite`), whichever form Microsoft echoes. Drive data (the drive id) is not read here.
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import aiohttp

from src.domain.connector_spec import ConnectorSpec
from src.domain.flow_intent import FlowIntent
from src.domain.oauth_credential import OAuthCredential
from src.domain.oauth_flow import OAuthFlow
from src.domain.oauth_grant import GrantResult
from src.domain.provider_account import ProviderAccount
from src.domain.revocation_scope import RevocationScope
from src.ports.oauth_provider_port import OAuthProviderPort
from src.utils.logger import logger

_TENANT_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0"
_AUTH_URL = f"{_TENANT_URL}/authorize"
_TOKEN_URL = f"{_TENANT_URL}/token"
_GRAPH_ME = "https://graph.microsoft.com/v1.0/me"
_GRAPH_PREFIX = "https://graph.microsoft.com/"
_REALM = "microsoft"
_IDENTITY_SCOPES = ("offline_access", "User.Read")
_TOKEN_TIMEOUT_S = 30
_ME_TIMEOUT_S = 10


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _short_scope(scope: str) -> str:
    return scope[len(_GRAPH_PREFIX):] if scope.startswith(_GRAPH_PREFIX) else scope


class MicrosoftOAuthProvider(OAuthProviderPort):
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def realm(self) -> str:
        return _REALM

    @property
    def revocation_scope(self) -> RevocationScope:
        return RevocationScope.NONE

    def authorization_url(self, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str, *, state: str) -> str:
        scopes = tuple(dict.fromkeys((*spec.required_scopes, *_IDENTITY_SCOPES)))
        params = {
            "client_id": self._client_id,
            "response_type": "code",
            "redirect_uri": redirect_uri,
            "scope": " ".join(scopes),
            "state": state,
            "response_mode": "query",
            "code_challenge": _pkce_challenge(flow.pkce_verifier),
            "code_challenge_method": "S256",
        }
        if flow.intent is FlowIntent.ADD:
            params["prompt"] = "select_account"
        if flow.intent is FlowIntent.RECONNECT and flow.hint_label:
            params["login_hint"] = flow.hint_label
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def exchange(self, code: str, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str) -> GrantResult:
        requested = " ".join(dict.fromkeys((*spec.required_scopes, *_IDENTITY_SCOPES)))
        payload = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "code": code,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "scope": requested,
            "code_verifier": flow.pkce_verifier,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                _TOKEN_URL, data=payload, timeout=aiohttp.ClientTimeout(total=_TOKEN_TIMEOUT_S)
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"Microsoft token exchange failed ({resp.status})")
                token_data = await resp.json()

            access_token = token_data["access_token"]
            async with session.get(
                _GRAPH_ME,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=aiohttp.ClientTimeout(total=_ME_TIMEOUT_S),
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"Microsoft account identity read failed ({resp.status})")
                body = await resp.json()

        subject = body.get("id") if isinstance(body, dict) else None
        if not isinstance(subject, str) or not subject:
            raise ValueError("Microsoft /me carried no id: no account identity")
        label = body.get("mail") or body.get("userPrincipalName") or ""
        granted = tuple(_short_scope(s) for s in str(token_data.get("scope") or "").split())

        logger.info(f"Microsoft OAuth exchange done: kind={spec.kind} granted={len(granted)} scopes")
        return GrantResult(
            access_token=access_token,
            refresh_token=token_data.get("refresh_token") or "",
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=int(token_data.get("expires_in", 3600))),
            scopes=granted,
            account=ProviderAccount(realm=_REALM, subject=subject, label=label if isinstance(label, str) else ""),
        )

    async def revoke(self, credential: OAuthCredential) -> None:
        """Microsoft is not revoked at the provider (`RevocationScope.NONE`): no HTTP call."""
        return None
