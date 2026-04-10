"""
GoogleOAuthService — generic incremental OAuth for any Google scope.

Renamed from GmailOAuthService (was gmail-specific).
Now accepts scopes as a constructor parameter — supports Gmail, Tasks, and any future
Google OAuth flow with the same code exchange logic.

Handles the "Connect Google Service" OAuth flow:
  1. Build Google OAuth authorization URL (specified scopes, offline access).
  2. Exchange authorization code for OAuthCredentials (access + refresh tokens).
  3. Revoke access token at Google on disconnect.

Uses aiohttp (already a project dependency). Not a port — one implementation,
called only by web endpoints in oauth_app.py and user_cabinet_app.py.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Optional
from urllib.parse import urlencode

import aiohttp

from ..domain.email import OAuthCredentials
from ..utils.logger import logger

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"

GMAIL_SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "openid",
    "email",
]


class GoogleOAuthService:
    """
    Generic Google OAuth service for incremental consent flows.

    Constructor args:
        client_id: Google OAuth 2.0 client ID.
        client_secret: Google OAuth 2.0 client secret.
        scopes: OAuth scopes to request. Defaults to GMAIL_SCOPES for backward compat.
    """

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        scopes: Optional[List[str]] = None,
    ):
        self._client_id = client_id
        self._client_secret = client_secret
        self._scopes = scopes if scopes is not None else GMAIL_SCOPES

    def get_authorization_url(self, state: str, redirect_uri: str) -> str:
        """Build Google OAuth URL requesting the configured scopes."""
        params = {
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(self._scopes),
            "state": state,
            "access_type": "offline",
            "prompt": "consent",   # Forces refresh_token on every connect
        }
        return f"{_AUTH_URL}?{urlencode(params)}"

    async def exchange_code(
        self,
        code: str,
        redirect_uri: str,
        user_id: str,
        provider_override: Optional[str] = None,
    ) -> OAuthCredentials:
        """
        Exchange authorization code for OAuthCredentials.

        Fetches email address from Google userinfo endpoint.
        Raises ValueError on HTTP errors.
        """
        payload = {
            "code": code,
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }

        async with aiohttp.ClientSession() as session:
            # 1. Exchange code → tokens
            async with session.post(_TOKEN_URL, data=payload) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ValueError(
                        f"Google token exchange failed ({resp.status}): {text}"
                    )
                token_data = await resp.json()

            access_token = token_data["access_token"]
            refresh_token = token_data.get("refresh_token", "")
            expires_in = int(token_data.get("expires_in", 3600))
            token_expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in)

            # 2. Fetch email from userinfo
            email_address = ""
            async with session.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
            ) as resp:
                if resp.status == 200:
                    info = await resp.json()
                    email_address = info.get("email", "")

        logger.info(
            f"✅ Google OAuth connected: email={email_address or '(unknown)'} "
            f"user={user_id[:8]}"
        )
        return OAuthCredentials(
            user_id=user_id,
            provider=provider_override or "gmail",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            scopes=self._scopes,
            email_address=email_address,
        )

    async def revoke_token(self, access_token: str) -> None:
        """Revoke access token at Google. Best-effort — errors are swallowed."""
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(
                    _REVOKE_URL,
                    params={"token": access_token},
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            logger.info("🗑️ Google token revoked at Google")
        except Exception as exc:
            logger.warning(f"⚠️ Google token revoke failed (ignored): {exc}")
