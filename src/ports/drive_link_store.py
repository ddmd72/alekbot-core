"""Storage contract for the drive connector's one-time download links (DRIVE_MCP_CONNECTOR_RFC §8).

Implemented by FirestoreDriveLinkStore; a fake in tests/support/drive_link_store.py. Used by DriveLinkService.
"""
from abc import ABC, abstractmethod
from datetime import datetime
from typing import Optional

from ..domain.drive_link import DriveLink


class DriveLinkStore(ABC):
    """
    Links keyed by `link_key`, the keyed digest of the raw id; the raw id never reaches the store.

    Holds for every method:
    - Tenancy: reads are by key only; the link carries its user, account and connection, which the caller re-checks.
    - Expiry: a link at or past `expires_at`, or with `reads >= max_reads`, is never returned by `peek` or
      `consume`; nothing is deleted on expiry (a TTL policy on `purge_after` deletes the record later).
    - Datetimes read back tz-aware UTC.
    - Errors: backend errors propagate; the caller refuses the link (fail closed).
    """

    @abstractmethod
    async def create(self, link: DriveLink) -> None:
        """Store `link` under link.link_key; never overwrites (an existing key raises the backend's conflict)."""

    @abstractmethod
    async def peek(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        """The link when it is live at `now`; None when absent, expired or out of reads. Writes nothing (HEAD)."""

    @abstractmethod
    async def consume(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        """One read of the link, in one transaction: None when absent, expired or out of reads, with nothing written;
        else `reads + 1` and `used_at = used_at or now` are written and the link is returned as it was before.

        Atomicity: of concurrent consumes of a link with one read left exactly one returns it.
        """
