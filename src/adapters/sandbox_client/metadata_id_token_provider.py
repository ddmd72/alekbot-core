"""
MetadataIdTokenProvider: the Google ID token the sandbox's caller gate checks (RFC §4.4, §4.10).

The token is minted by the metadata server through `google.oauth2.id_token.fetch_id_token`, a blocking call, so it
runs in `asyncio.to_thread` (the `http_call_control_plane_adapter` pattern). New beyond the relay: the token is
cached until five minutes before its `exp`. The audience is the sandbox URL exactly as the gate compares it (equality,
no trailing slash): it is normalised once here and the client uses the same string for its base URL.
The token is never logged.
"""
from __future__ import annotations

import asyncio
import base64
import json
import time
from typing import Callable, Optional

from ...utils.logger import logger

REFRESH_MARGIN_S = 300.0


def normalise_audience(url: str) -> str:
    return url.strip().rstrip("/")


def _fetch_from_metadata(audience: str) -> str:
    import google.auth.transport.requests
    import google.oauth2.id_token

    return google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), audience)


def _expiry(token: str) -> Optional[float]:
    """`exp` of a JWT payload (unverified: we only schedule our own refresh), or None if unreadable."""
    try:
        payload = token.split(".")[1]
        raw = base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))
        exp = json.loads(raw).get("exp")
        return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else None
    except (IndexError, ValueError, UnicodeDecodeError):
        return None


class MetadataIdTokenProvider:
    def __init__(self, audience: str, *, fetch: Callable[[str], str] = _fetch_from_metadata,
                 clock: Callable[[], float] = time.time) -> None:
        self._audience = normalise_audience(audience)
        self._fetch = fetch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._token: Optional[str] = None
        self._refresh_at = 0.0

    @property
    def audience(self) -> str:
        return self._audience

    async def __call__(self) -> str:
        async with self._lock:
            if self._token is not None and self._clock() < self._refresh_at:
                return self._token
            try:
                token = await asyncio.to_thread(self._fetch, self._audience)
            except Exception as exc:
                logger.error("[SandboxClient] id token fetch failed: %s", type(exc).__name__)
                raise
            exp = _expiry(token)
            self._token = token
            self._refresh_at = (exp - REFRESH_MARGIN_S) if exp is not None else 0.0
            return token
