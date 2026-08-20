"""
Short Link Service.

Mints short, copy-paste-friendly `/s/<code>` aliases for long capability
links (e.g. `/f/<token>`). The code carries no meaning by itself — it is
purely a pointer stored via ShortLinkRepositoryPort; resolving it back to
the target URL happens in the `/s/<code>` web route.

Generic on purpose: doesn't know about files, tokens, or gating. Any long
URL can be shortened.
"""
from __future__ import annotations

import secrets
import string
from datetime import datetime, timedelta, timezone
from typing import Optional

from ..domain.short_link import ShortLink
from ..ports.short_link_repository_port import ShortLinkRepositoryPort
from ..utils.logger import logger

_CODE_ALPHABET = string.ascii_letters + string.digits  # base62
_CODE_LENGTH = 10
_MAX_MINT_ATTEMPTS = 5


class ShortLinkService:
    """Mint/store short code aliases for long URLs."""

    def __init__(self, repository: ShortLinkRepositoryPort, base_url: str) -> None:
        self._repo = repository
        self._base_url = base_url.rstrip("/")

    async def shorten(self, target_url: str, ttl_seconds: int) -> str:
        """
        Mint a short alias for target_url, valid for ttl_seconds.

        Returns:
            "<base_url>/s/<code>"

        Raises:
            RuntimeError: retry budget exhausted on repeated code collisions
                (astronomically unlikely at 10-char base62 — signals a
                deeper problem, e.g. a broken RNG, rather than bad luck).
        """
        expires_at = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)
        for attempt in range(_MAX_MINT_ATTEMPTS):
            code = self._generate_code()
            link = ShortLink(code=code, target_url=target_url, expires_at=expires_at)
            if await self._repo.create_if_absent(link):
                return f"{self._base_url}/s/{code}"
            logger.warning("ShortLinkService: code collision on attempt %d", attempt + 1)
        raise RuntimeError(
            f"ShortLinkService: exhausted {_MAX_MINT_ATTEMPTS} attempts minting a unique code"
        )

    @staticmethod
    def _generate_code() -> str:
        return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))

    async def resolve(self, code: str) -> Optional[str]:
        """Return the target URL for a code, or None if missing/expired."""
        link = await self._repo.resolve(code)
        return link.target_url if link else None
