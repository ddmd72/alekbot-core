"""
File Access Token Service.

Mints and verifies short-/long-lived capability tokens that gate access to
private GCS objects. A token is an HS256 JWT carrying the object key, the owning
user_id, and an expiry. The user-facing link is `https://<domain>/f/<token>`;
the `/f/<token>` web route verifies the token and 302-redirects to a freshly
minted GCS V4 signed URL (5 min). The bucket itself is private — the token is
the only thing that grants access.

Why a separate token (not the GCS signed URL directly): GCS V4 signatures are
capped at 7 days, which is too short for "open the link a month later". Our JWT
carries the real TTL (e.g. 30 days), and a short signed URL is minted on each
click — so the long-lived link never embeds a long-lived storage signature.

Reuses OAUTH_SESSION_SECRET (same secret as SessionService) for HS256 signing.

`gated=True` marks the most sensitive artifacts (daily email review): the
`/f/<token>` route additionally requires a valid Cabinet JWT cookie for those.
"""
from __future__ import annotations

import jwt
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..domain.file_access import DEFAULT_FILE_LINK_TTL, EMAIL_REVIEW_FILE_LINK_TTL
from ..utils.logger import logger


class FileAccessTokenError(Exception):
    """Base class for file-access-token failures."""


class FileAccessTokenInvalid(FileAccessTokenError):
    """Token signature is invalid, malformed, or the wrong type."""


class FileAccessTokenExpired(FileAccessTokenError):
    """Token has passed its expiry."""


@dataclass(frozen=True)
class FileAccessToken:
    """Decoded, verified file-access token payload (value object)."""

    key: str          # GCS object key, e.g. "docs/<uuid>-report.pdf"
    user_id: str      # owner — must match the requester for sensitive paths
    gated: bool       # True → /f route also requires a valid Cabinet cookie


class FileAccessTokenService:
    """Mint/verify HS256 capability tokens for private file access."""

    _ALGORITHM = "HS256"
    _TYPE = "file_access"

    # Link lifetimes (seconds) — sourced from domain policy. Class aliases kept for
    # convenience/back-compat; FileLinkService reads the domain constants directly.
    DEFAULT_TTL = DEFAULT_FILE_LINK_TTL          # 30 days — docs/html/deep_research/uploads
    EMAIL_REVIEW_TTL = EMAIL_REVIEW_FILE_LINK_TTL  # 5 days — daily email review (PII)

    def __init__(self, secret_key: str) -> None:
        """
        Args:
            secret_key: HS256 signing secret (min 32 chars) — OAUTH_SESSION_SECRET.

        Raises:
            ValueError: If secret_key is too short.
        """
        if not secret_key or len(secret_key) < 32:
            raise ValueError("file-access token secret must be at least 32 characters")
        self._secret = secret_key

    def mint(
        self,
        key: str,
        user_id: str,
        ttl_seconds: int = DEFAULT_TTL,
        gated: bool = False,
    ) -> str:
        """
        Mint a capability token for a private object.

        Args:
            key:         GCS object key the token grants access to.
            user_id:     Owning user; the /f route enforces it on sensitive paths.
            ttl_seconds: Link lifetime. Independent of the GCS signed-URL ceiling.
            gated:       True → /f route additionally requires a Cabinet cookie.

        Returns:
            Encoded JWT string.
        """
        now = datetime.now(timezone.utc)
        payload = {
            "key": key,
            "uid": user_id,
            "gated": gated,
            "type": self._TYPE,
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(seconds=ttl_seconds)).timestamp()),
        }
        return jwt.encode(payload, self._secret, algorithm=self._ALGORITHM)

    def verify(self, token: str) -> FileAccessToken:
        """
        Verify a token and return its decoded payload.

        Raises:
            FileAccessTokenExpired: Token past expiry.
            FileAccessTokenInvalid: Bad signature / malformed / wrong type / missing claims.
        """
        try:
            payload = jwt.decode(token, self._secret, algorithms=[self._ALGORITHM])
        except jwt.ExpiredSignatureError as e:
            raise FileAccessTokenExpired("file access token expired") from e
        except jwt.InvalidTokenError as e:
            raise FileAccessTokenInvalid(f"invalid file access token: {e}") from e

        if payload.get("type") != self._TYPE:
            raise FileAccessTokenInvalid(
                f"wrong token type: {payload.get('type')!r}"
            )
        key = payload.get("key")
        uid = payload.get("uid")
        if not key or not uid:
            raise FileAccessTokenInvalid("token missing key/uid claim")

        return FileAccessToken(key=key, user_id=uid, gated=bool(payload.get("gated", False)))
