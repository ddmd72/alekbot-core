"""
ShortLinkRepositoryPort — persistence boundary for short-code → URL mappings.

Used by ShortLinkService to store minted codes and by the `/s/<code>` web
route to resolve them back to a target URL.
"""

from abc import ABC, abstractmethod
from typing import Optional

from src.domain.short_link import ShortLink


class ShortLinkRepositoryPort(ABC):

    @abstractmethod
    async def create_if_absent(self, link: ShortLink) -> bool:
        """
        Persist a short link, but only if its code isn't already taken.

        Returns:
            True if created, False if the code already exists (caller should
            mint a new code and retry).
        """

    @abstractmethod
    async def resolve(self, code: str) -> Optional[ShortLink]:
        """Return the stored link for a code, or None if not found."""
