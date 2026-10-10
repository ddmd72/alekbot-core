"""
MicrosoftGraphTokenProvider — bearer headers for Microsoft Graph (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Shared by every Microsoft Graph adapter (To Do, the user drive). One credentials record per
`provider` key with its own scope, so integrations are revoked independently. The access
token is cached in memory for at most 5 minutes: one credentials-store read per 5 minutes,
not per HTTP call, and a disconnect or reconnect reaches every instance within that time.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.email import OAuthCredentials
from ...ports.oauth_credentials_port import OAuthCredentialsPort
from ...utils.logger import logger

_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_REFRESH_MARGIN = timedelta(minutes=5)
CACHE_TTL = timedelta(minutes=5)  # owner decision (USER_DRIVE_RFC §4.2): disconnect/reconnect seen within 5 min


class GraphReauthRequired(ValueError):
    """The refresh token is no longer accepted (expired, revoked): the user must reconnect."""


class MicrosoftGraphTokenProvider:
    def __init__(self, oauth: OAuthCredentialsPort, client_id: str, client_secret: str,
                 provider: str, scope: str) -> None:
        self._oauth = oauth
        self._client_id = client_id
        self._client_secret = client_secret
        self._provider = provider
        self._scope = scope
        # user_id → (access_token, token_expiry, fetched_at)
        self._cache: Dict[str, Tuple[str, datetime, datetime]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}

    def invalidate(self, user_id: str) -> None:
        self._cache.pop(user_id, None)

    def _fresh(self, user_id: str, *, fetched_since: Optional[datetime] = None) -> Optional[str]:
        """The cached token when it is still usable; with `fetched_since`, only one fetched at or after it."""
        cached = self._cache.get(user_id)
        now = datetime.now(timezone.utc)
        if cached and cached[1] > now + _REFRESH_MARGIN and cached[2] > now - CACHE_TTL:
            if fetched_since is None or cached[2] >= fetched_since:
                return cached[0]
        return None

    async def headers(self, user_id: str, *, force_refresh: bool = False) -> Optional[Dict[str, str]]:
        """Authorization header, or None when the user has not connected this provider.

        `force_refresh` (the adapters' 401 path) refreshes even a cached token — but one refresh
        serves every caller that was waiting on it, so a burst of 401s is one token exchange.
        """
        token = None if force_refresh else self._fresh(user_id)
        if token:
            return {"Authorization": f"Bearer {token}"}
        requested_at = datetime.now(timezone.utc)
        async with self._locks.setdefault(user_id, asyncio.Lock()):
            token = self._fresh(user_id, fetched_since=requested_at if force_refresh else None)
            if token:
                return {"Authorization": f"Bearer {token}"}
            creds = await self._oauth.get_credentials(user_id, self._provider)
            if creds is None:
                self._cache.pop(user_id, None)
                return None
            if force_refresh or creds.token_expiry <= datetime.now(timezone.utc) + _REFRESH_MARGIN:
                creds = await self.refresh(creds)
            self._cache[user_id] = (creds.access_token, creds.token_expiry, datetime.now(timezone.utc))
            return {"Authorization": f"Bearer {creds.access_token}"}

    async def refresh(self, creds: OAuthCredentials) -> OAuthCredentials:
        """Exchange the refresh token for a new access token and persist it."""
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": self._scope,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=data) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logger.error(f"MS token refresh failed ({self._provider}, {resp.status}): {body[:300]}")
                    if "invalid_grant" in body:
                        raise GraphReauthRequired(f"MS token refresh rejected ({self._provider}): reconnect needed")
                    raise ValueError(f"MS token refresh failed ({resp.status}): {body}")
                payload = await resp.json()

        new_creds = OAuthCredentials(
            user_id=creds.user_id,
            provider=self._provider,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", creds.refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=payload.get("expires_in", 3600)),
            scopes=creds.scopes,
            email_address=creds.email_address,
        )
        await self._oauth.save_credentials(new_creds)
        logger.info(f"🔄 MS token refreshed ({self._provider}) for user {creds.user_id[:8]}")
        return new_creds
