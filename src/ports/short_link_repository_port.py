"""
ShortLinkRepositoryPort — persistence boundary for short-code → URL mappings.

Used by ShortLinkService to store minted codes and by the `/s/<code>` web
route to resolve them back to a target URL.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.short_link import ShortLink


class ShortLinkRepositoryPort(ABC):
    """Short code -> target URL mappings, one per code.

    Tenancy: none; the code is the capability. Backend errors propagate.
    """

    @abstractmethod
    async def create_if_absent(self, link: ShortLink) -> bool:
        """
        Persist a short link, but only if its code isn't already taken.

        Atomicity: of two concurrent creates of one code exactly one returns True and its link is the stored one. A
        code stays taken after its link expired, until the backend removes it.

        Returns:
            True if created, False if the code already exists (caller should
            mint a new code and retry).
        """

    @abstractmethod
    async def resolve(self, code: str) -> Optional[ShortLink]:
        """The stored link, or None when the code is absent or its link expired (expires_at at or before now).

        A stored link without expires_at or target_url: no writer produces one; behaviour pending the data audit.
        """
