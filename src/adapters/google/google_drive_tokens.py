"""GoogleDriveTokenProvider — access tokens of the Google drive grants (GOOGLE_DRIVE_RFC §3.1, GE-16).

The pattern of `MicrosoftGraphTokenProvider` (`src/adapters/microsoft/graph_auth.py`), written again here because an
adapter imports no other adapter subpackage (REQ-ARCH-23) and Gmail's refresh is not keyed by credential:
- a cache per `(user_id, credential_id)` held at most `CACHE_TTL` (5 minutes): a disconnect or reconnect is seen
  within that time;
- one `asyncio.Lock` per key: concurrent refreshes of one grant are one token call;
- the refreshed token is written back through `OAuthCredentialsPort.update` (a record deleted meanwhile is not
  re-created: not connected);
- a rejected refresh (`invalid_grant`) is `DriveReauthRequired`; any other token-endpoint failure (a non-200, a
  transport error, a timeout) is `DriveThrottled`: systemic, never charged to the item being read.

`token(...)` returns the access token and its expiry (the expiry is what `media_source` reports); `min_life` asks for
a token that lives at least that long, refreshing first otherwise (`MEDIA_TOKEN_MIN_LIFE`). Tokens, refresh tokens and
the endpoint's free text are never logged.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional, Tuple

import aiohttp  # module import on purpose: tests patch aiohttp.ClientSession

from ...domain.oauth_credential import OAuthCredential
from ...domain.user_drive import DriveReauthRequired, DriveThrottled
from ...ports.oauth_credentials_port import OAuthCredentialsPort
from ...utils.logger import logger

Key = Tuple[str, str]   # (user_id, credential_id)

TOKEN_URL = "https://oauth2.googleapis.com/token"
CACHE_TTL = timedelta(minutes=5)            # §3.1: as the Graph provider (USER_DRIVE_RFC §4.2)
REFRESH_MARGIN = timedelta(minutes=5)       # a token living less than this is refreshed before use
TOKEN_POST_TIMEOUT_S = 30.0                 # own bound on the token POST (aiohttp's default is 300 s)
THROTTLE_HINT_S = 60.0                      # a failed refresh: the tick retries after this
_REAUTH_ERRORS = ("invalid_grant",)         # only a new consent clears it


def _oauth_error(body: str) -> str:
    """The `error` code of an OAuth error body (an enum such as invalid_grant); "" when absent or not one."""
    try:
        code = str((json.loads(body) or {}).get("error") or "")
    except (ValueError, AttributeError, TypeError):
        return ""
    return code if re.fullmatch(r"[a-z_]{1,64}", code) else ""


class GoogleDriveTokenProvider:
    """Access tokens per stored credential, keyed by (user_id, credential_id) (MULTI_DRIVE_RFC §5.1, T-3). `kind` is
    the connector kind of the adapter's injected spec: the credential family it refreshes, named in its logs."""

    def __init__(self, oauth: OAuthCredentialsPort, client_id: str, client_secret: str, kind: str) -> None:
        self._oauth = oauth
        self._client_id = client_id
        self._client_secret = client_secret
        self._kind = kind
        # (user_id, credential_id) → (access_token, token_expiry, fetched_at)
        self._cache: Dict[Key, Tuple[str, datetime, datetime]] = {}
        self._locks: Dict[Key, asyncio.Lock] = {}

    def invalidate(self, user_id: str, credential_id: str) -> None:
        """Drop the cached token and the lock of one grant (after a disconnect: `forget`)."""
        self._cache.pop((user_id, credential_id), None)
        lock = self._locks.get((user_id, credential_id))
        if lock is not None and not lock.locked():
            self._locks.pop((user_id, credential_id), None)

    def holds(self, user_id: str, credential_id: str) -> bool:
        """True while a token or a lock of the grant is held."""
        key = (user_id, credential_id)
        return key in self._cache or key in self._locks

    def _fresh(self, key: Key, min_life: timedelta, fetched_since: Optional[datetime] = None
               ) -> Optional[Tuple[str, datetime]]:
        cached = self._cache.get(key)
        now = datetime.now(timezone.utc)
        if cached and cached[1] > now + min_life and cached[2] > now - CACHE_TTL:
            if fetched_since is None or cached[2] >= fetched_since:
                return cached[0], cached[1]
        return None

    async def token(self, user_id: str, credential_id: str, *, force_refresh: bool = False,
                    min_life: timedelta = REFRESH_MARGIN) -> Optional[Tuple[str, datetime]]:
        """(access token, expiry) of the grant, or None when it is not stored (not connected). `force_refresh` (the
        401 path) refreshes even a cached token, but one refresh serves every caller waiting on it."""
        key = (user_id, credential_id)
        hit = None if force_refresh else self._fresh(key, min_life)
        if hit:
            return hit
        requested_at = datetime.now(timezone.utc)
        async with self._locks.setdefault(key, asyncio.Lock()):
            hit = self._fresh(key, min_life, fetched_since=requested_at if force_refresh else None)
            if hit:
                return hit
            creds = await self._oauth.get(user_id, credential_id)
            if creds is None:
                self._cache.pop(key, None)
                return None
            if force_refresh or creds.token_expiry <= datetime.now(timezone.utc) + min_life:
                refreshed = await self._refresh(creds)
                if refreshed is None:        # disconnected while the token endpoint answered
                    self._cache.pop(key, None)
                    return None
                creds = refreshed
            self._cache[key] = (creds.access_token, creds.token_expiry, datetime.now(timezone.utc))
            return creds.access_token, creds.token_expiry

    async def _refresh(self, creds: OAuthCredential) -> Optional[OAuthCredential]:
        """Exchange the refresh token and persist the new access token; None when the record was deleted meanwhile."""
        data = {"client_id": self._client_id, "client_secret": self._client_secret,
                "refresh_token": creds.refresh_token, "grant_type": "refresh_token"}
        who = creds.credential_id[:8]
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(TOKEN_URL, data=data,
                                        timeout=aiohttp.ClientTimeout(total=TOKEN_POST_TIMEOUT_S)) as resp:
                    if resp.status != 200:
                        code = _oauth_error(await resp.text())
                        # status and the enum-like OAuth `error` only: the body's free text can quote the account
                        logger.warning(f"Google drive token refresh failed ({self._kind}, {resp.status}): {code} "
                                       f"for credential {who}")
                        if code in _REAUTH_ERRORS:
                            raise DriveReauthRequired("Drive access expired; reconnect needed")
                        raise DriveThrottled(THROTTLE_HINT_S)
                    payload = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning(f"Google drive token refresh failed ({self._kind}): {type(exc).__name__} "
                           f"for credential {who}")
            raise DriveThrottled(THROTTLE_HINT_S) from exc
        access = payload.get("access_token") if isinstance(payload, dict) else None
        if not isinstance(access, str) or not access:
            logger.warning(f"Google drive token refresh answered without a token ({self._kind}) for credential {who}")
            raise DriveThrottled(THROTTLE_HINT_S)
        try:
            lifetime = int(payload.get("expires_in", 3600))
        except (TypeError, ValueError):
            lifetime = 3600
        new_creds = replace(
            creds,
            access_token=access,
            refresh_token=payload.get("refresh_token") or creds.refresh_token,
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=lifetime),
            needs_reconnect=False,
        )
        if not await self._oauth.update(new_creds):
            logger.info(f"Google drive token refreshed but the grant is gone ({self._kind}) for credential {who}")
            return None
        logger.info(f"🔄 Google drive token refreshed ({self._kind}) for credential {who}")
        return new_creds
