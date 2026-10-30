"""
MicrosoftGraphTokenProvider — bearer headers for Microsoft Graph (docs/10_rfcs/USER_DRIVE_RFC.md §4.2).

Shared by every Microsoft Graph adapter (To Do, the user drive). Each integration has its own
credential and scope, so integrations are revoked independently. Tokens are cached per
(user_id, credential_id) (MULTI_DRIVE_RFC §5.1) in memory for at most 5 minutes: one
credentials-store read per 5 minutes, not per HTTP call, and a disconnect or reconnect reaches
every instance within that time.
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
from ...ports.oauth_credentials_port import OAuthCredentialsPort
from ...utils.logger import logger

Key = Tuple[str, str]   # (user_id, credential_id)

_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
_REFRESH_MARGIN = timedelta(minutes=5)
CACHE_TTL = timedelta(minutes=5)  # owner decision (USER_DRIVE_RFC §4.2): disconnect/reconnect seen within 5 min
# Token-endpoint error codes only a new sign-in clears: a dead grant, MFA or Conditional Access asking for
# interaction, consent withdrawn. Retrying them only repeats the refusal (USER_DRIVE_INDEXING_RFC §7.7).
_REAUTH_ERRORS = ("invalid_grant", "interaction_required", "consent_required")
# Own bound on the token POST (aiohttp's default is 300 s): a hanging endpoint ends as a TimeoutError well
# inside a Graph call's budget. Callers keep their mapping of it (the drive: a systemic throttle).
TOKEN_POST_TIMEOUT_S = 30.0


def _oauth_error(body: str) -> str:
    """The `error` code of an OAuth error body (an enum such as invalid_grant); '' when absent or not one."""
    try:
        code = str((json.loads(body) or {}).get("error") or "")
    except (ValueError, AttributeError, TypeError):
        return ""
    return code if re.fullmatch(r"[a-z_]{1,64}", code) else ""


class GraphReauthRequired(ValueError):
    """The refresh token is no longer accepted (expired, revoked, a sign-in or consent is required):
    the user must reconnect."""


class MicrosoftGraphTokenProvider:
    """Bearer headers per stored credential, keyed by (user_id, credential_id) (MULTI_DRIVE_RFC §5.1, E3).

    `kind` is the connector kind of the adapter's injected spec; it serves only `user_headers`, the
    single-credential lookup To Do and the drive use until slice 1b passes the credential by handle.
    """

    def __init__(self, oauth: OAuthCredentialsPort, client_id: str, client_secret: str,
                 kind: str, scope: str) -> None:
        self._oauth = oauth
        self._client_id = client_id
        self._client_secret = client_secret
        self._kind = kind
        self._scope = scope
        # (user_id, credential_id) → (access_token, token_expiry, fetched_at)
        self._cache: Dict[Key, Tuple[str, datetime, datetime]] = {}
        self._locks: Dict[Key, asyncio.Lock] = {}
        # user_id → (credential_id, resolved_at): the single-credential lookup, held as long as a token (CACHE_TTL)
        self._credential_ids: Dict[str, Tuple[str, datetime]] = {}
        self._id_locks: Dict[str, asyncio.Lock] = {}

    def invalidate(self, user_id: str, credential_id: str) -> None:
        self._cache.pop((user_id, credential_id), None)

    def forget(self, user_id: str) -> None:
        """Drop the user's single-credential lookup and every token cached for the user (a disconnect, or a grant
        that changed under this process)."""
        self._credential_ids.pop(user_id, None)
        for key in [k for k in self._cache if k[0] == user_id]:
            self._cache.pop(key, None)

    def held_credential_id(self, user_id: str) -> Optional[str]:
        """The credential id `credential_id_of` holds for the user, expired or not (None when none is held)."""
        hit = self._credential_ids.get(user_id)
        return hit[0] if hit else None

    def cached_token(self, user_id: str, credential_id: str) -> Optional[str]:
        """The access token cached for the credential, fresh or not (None when nothing is cached).
        Lets an adapter notice that the stored grant changed under it (a reconnect elsewhere)."""
        cached = self._cache.get((user_id, credential_id))
        return cached[0] if cached else None

    def _fresh(self, key: Key, *, fetched_since: Optional[datetime] = None) -> Optional[str]:
        """The cached token when it is still usable; with `fetched_since`, only one fetched at or after it."""
        cached = self._cache.get(key)
        now = datetime.now(timezone.utc)
        if cached and cached[1] > now + _REFRESH_MARGIN and cached[2] > now - CACHE_TTL:
            if fetched_since is None or cached[2] >= fetched_since:
                return cached[0]
        return None

    async def credential_id_of(self, user_id: str) -> Optional[str]:
        """The id of the user's one credential of `kind` (the oldest when several), or None when there is none.
        Held for CACHE_TTL, like a token: a disconnect or reconnect is seen within that time."""
        now = datetime.now(timezone.utc)
        hit = self._credential_ids.get(user_id)
        if hit and hit[1] > now - CACHE_TTL:
            return hit[0]
        async with self._id_locks.setdefault(user_id, asyncio.Lock()):
            hit = self._credential_ids.get(user_id)
            if hit and hit[1] > now - CACHE_TTL:
                return hit[0]
            found = await self._oauth.list_for_user(user_id, self._kind)
            if not found:
                self._credential_ids.pop(user_id, None)
                return None
            credential_id = found[0].credential_id
            self._credential_ids[user_id] = (credential_id, datetime.now(timezone.utc))
            return credential_id

    async def user_headers(self, user_id: str, *, force_refresh: bool = False) -> Optional[Dict[str, str]]:
        """`headers` for the user's one credential of `kind`; None when the user has not connected it. A held
        credential id whose record is gone (a reconnect of another account) is looked up again once."""
        for _ in range(2):
            credential_id = await self.credential_id_of(user_id)
            if credential_id is None:
                return None
            headers = await self.headers(user_id, credential_id, force_refresh=force_refresh)
            if headers is not None:
                return headers
            self._credential_ids.pop(user_id, None)
        return None

    async def headers(self, user_id: str, credential_id: str, *,
                      force_refresh: bool = False) -> Optional[Dict[str, str]]:
        """Authorization header, or None when the credential is not stored (not connected).

        `force_refresh` (the adapters' 401 path) refreshes even a cached token — but one refresh
        serves every caller that was waiting on it, so a burst of 401s is one token exchange.
        """
        key = (user_id, credential_id)
        token = None if force_refresh else self._fresh(key)
        if token:
            return {"Authorization": f"Bearer {token}"}
        requested_at = datetime.now(timezone.utc)
        async with self._locks.setdefault(key, asyncio.Lock()):
            token = self._fresh(key, fetched_since=requested_at if force_refresh else None)
            if token:
                return {"Authorization": f"Bearer {token}"}
            creds = await self._oauth.get(user_id, credential_id)
            if creds is None:
                self._cache.pop(key, None)
                return None
            if force_refresh or creds.token_expiry <= datetime.now(timezone.utc) + _REFRESH_MARGIN:
                refreshed = await self.refresh(creds)
                if refreshed is None:        # disconnected while the token endpoint answered: not connected
                    self._cache.pop(key, None)
                    return None
                creds = refreshed
            self._cache[key] = (creds.access_token, creds.token_expiry, datetime.now(timezone.utc))
            return {"Authorization": f"Bearer {creds.access_token}"}

    async def refresh(self, creds: OAuthCredential) -> Optional[OAuthCredential]:
        """Exchange the refresh token for a new access token and persist it. None when the record was
        deleted meanwhile (a disconnect): nothing is stored, the grant stays gone."""
        data = {
            "client_id": self._client_id,
            "client_secret": self._client_secret,
            "refresh_token": creds.refresh_token,
            "grant_type": "refresh_token",
            "scope": self._scope,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(_TOKEN_URL, data=data,
                                    timeout=aiohttp.ClientTimeout(total=TOKEN_POST_TIMEOUT_S)) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    # status and the enum-like OAuth `error` code only: the body's free text can quote the account
                    logger.error(f"MS token refresh failed ({self._kind}, {resp.status}): {_oauth_error(body)}")
                    if any(code in body for code in _REAUTH_ERRORS):
                        raise GraphReauthRequired(f"MS token refresh rejected ({self._kind}): reconnect needed")
                    raise ValueError(f"MS token refresh failed ({resp.status})")
                payload = await resp.json()

        # Every other field is carried over, so the identity (credential_id, account_id, integration, account) and the
        # connected drive's id survive the refresh (USER_DRIVE_INDEXING_RFC §10.1 step 0). needs_reconnect is
        # cleared: a refresh that succeeded is the proof it no longer holds.
        new_creds = replace(
            creds,
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", creds.refresh_token),
            token_expiry=datetime.now(timezone.utc) + timedelta(seconds=payload.get("expires_in", 3600)),
            needs_reconnect=False,
        )
        if not await self._oauth.update(new_creds):
            logger.info(f"MS token refreshed but the grant is gone ({self._kind}) for user {creds.user_id[:8]}")
            return None
        logger.info(f"🔄 MS token refreshed ({self._kind}) for user {creds.user_id[:8]}")
        return new_creds
