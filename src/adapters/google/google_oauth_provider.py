"""GoogleOAuthProvider — the OAuth edge of the Google realm (MULTI_DRIVE_RFC §5.1, §5.2).

Builds the consent URL (PKCE S256, offline access), exchanges the code for tokens plus the consenting account
(OpenID `sub` and email from userinfo) and revokes at Google. It checks no required scope: it returns the granted
scopes and the connect service decides. Token refresh is not here (E3).
"""
import base64
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Tuple
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

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_REALM = "google"
# the account identity is read from userinfo on every consent (SECRETS_AT_REST_RFC §2.3, I9)
_IDENTITY_SCOPES = ("openid", "email")
_HTTP_TIMEOUT_S = 30


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _timeout() -> aiohttp.ClientTimeout:
    return aiohttp.ClientTimeout(total=_HTTP_TIMEOUT_S)


class GoogleOAuthProvider(OAuthProviderPort):
    def __init__(self, client_id: str, client_secret: str) -> None:
        self._client_id = client_id
        self._client_secret = client_secret

    @property
    def realm(self) -> str:
        return _REALM

    @property
    def revocation_scope(self) -> RevocationScope:
        return RevocationScope.GRANT_WIDE

    def authorization_url(self, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str, *, state: str) -> str:
        scopes: Tuple[str, ...] = tuple(dict.fromkeys((*spec.required_scopes, *_IDENTITY_SCOPES)))
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent select_account" if flow.intent is FlowIntent.ADD else "consent",
            "code_challenge": _pkce_challenge(flow.pkce_verifier),
            "code_challenge_method": "S256",
        }
        if flow.intent is FlowIntent.RECONNECT and flow.hint_label:
            params["login_hint"] = flow.hint_label
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def exchange(self, code: str, flow: OAuthFlow, spec: ConnectorSpec, redirect_uri: str) -> GrantResult:
        payload = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
            "code_verifier": flow.pkce_verifier,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=payload, timeout=_timeout()) as resp:
                if resp.status != 200:
                    raise ValueError(f"Google token exchange failed ({resp.status})")
                token_data = await resp.json()

            access_token = token_data["access_token"]
            refresh_token = token_data.get("refresh_token", "")
            expires_in = int(token_data.get("expires_in", 3600))
            granted = tuple(str(token_data.get("scope") or "").split())

            async with session.get(
                _USERINFO_URL, headers={"Authorization": f"Bearer {access_token}"}, timeout=_timeout()
            ) as resp:
                if resp.status != 200:
                    raise ValueError(f"Google userinfo read failed ({resp.status}): no account identity")
                info = await resp.json()

        subject = info.get("sub") if isinstance(info, dict) else None
        email_address = info.get("email") if isinstance(info, dict) else None
        if not isinstance(subject, str) or not subject or not isinstance(email_address, str) or not email_address:
            raise ValueError("Google userinfo carried no sub or email: no account identity")

        logger.info(f"Google OAuth exchange done: kind={spec.kind} granted={len(granted)} scopes")
        return GrantResult(
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=expires_in),
            scopes=granted,
            account=ProviderAccount(realm=_REALM, subject=subject, label=email_address),
        )

    async def revoke(self, credential: OAuthCredential) -> None:
        """Best-effort: a failure is logged and never raised."""
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    _REVOKE_URL,
                    params={"token": credential.access_token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    timeout=_timeout(),
                )
            logger.info("Google token revoked at Google")
        except Exception as exc:
            logger.warning(f"Google token revoke failed (ignored): {type(exc).__name__}")
