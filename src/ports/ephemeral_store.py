"""
EphemeralStore port — shared, short-TTL key/value store.

Backs both the voice call ticket and the RFC §3 one-call-per-user marker
(RFC §4.5, §3): same store, same TTL semantics, different keys/collections.
"""
from abc import ABC, abstractmethod
from typing import Optional


class EphemeralStore(ABC):
    """A shared, short-TTL key/value store. Backs both the call ticket and
    the §3 one-call-per-user marker - same store, same TTL semantics (RFC
    §4.5, §3)."""

    @abstractmethod
    async def set(self, key: str, value: dict, ttl_s: int) -> None: ...

    @abstractmethod
    async def get(self, key: str) -> Optional[dict]:
        """Returns None if the key is missing or past its TTL."""

    @abstractmethod
    async def delete(self, key: str) -> None: ...
