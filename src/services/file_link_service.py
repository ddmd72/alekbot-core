"""
File Link Service.

Single funnel that turns a stored object key into a user-facing capability link
(`<base>/f/<token>`). Centralizes the policy that maps an object key to its
token parameters:

  - email_review/  → gated (Cabinet cookie required at /f) + short 5-day TTL
  - everything else (docs/, html/, deep_research/, {user_id}/files/) → 30-day TTL, not gated

Keeping this policy in one place means storage adapters stay dumb (they return
keys) and every delivery caller produces consistent links.
"""
from __future__ import annotations

from .file_access_token_service import FileAccessTokenService
from ..utils.logger import logger

# Object-key prefix for the most sensitive artifact (daily email review).
_EMAIL_REVIEW_PREFIX = "email_review/"


class FileLinkService:
    """Build capability links for private stored objects."""

    def __init__(self, token_service: FileAccessTokenService, base_url: str) -> None:
        """
        Args:
            token_service: Mints the HS256 capability token.
            base_url:      Public base URL where /f/<token> is served
                           (CLOUD_RUN_SERVICE_URL), e.g. "https://dev.alekbot.app".
        """
        self._tokens = token_service
        self._base_url = base_url.rstrip("/")

    def build_link(self, key: str, user_id: str) -> str:
        """
        Build a capability link for a stored object.

        TTL and gating are derived from the key's prefix:
          - email_review/ → gated + 5-day TTL
          - else          → 30-day TTL, not gated

        Args:
            key:     Stored object key (as returned by MediaStoragePort.store()).
            user_id: Owning user — embedded in the token and enforced at /f.

        Returns:
            "<base_url>/f/<token>"
        """
        gated = key.startswith(_EMAIL_REVIEW_PREFIX)
        ttl = (
            FileAccessTokenService.EMAIL_REVIEW_TTL
            if gated
            else FileAccessTokenService.DEFAULT_TTL
        )
        token = self._tokens.mint(key=key, user_id=user_id, ttl_seconds=ttl, gated=gated)
        logger.info(
            "FileLinkService: link for '%s' (user=%s, gated=%s, ttl=%ds)",
            key, user_id[:8] if user_id else "?", gated, ttl,
        )
        return f"{self._base_url}/f/{token}"
