"""An in-memory `DriveLinkStore` with the port's semantics (create never overwrites, peek writes nothing, consume is
atomic: no await between its read and its write, so of concurrent consumes on one event loop exactly one wins)."""
from dataclasses import replace
from datetime import datetime
from typing import Dict, List, Optional

from google.api_core.exceptions import AlreadyExists

from src.domain.drive_link import DriveLink
from src.ports.drive_link_store import DriveLinkStore


class FakeDriveLinkStore(DriveLinkStore):
    def __init__(self) -> None:
        self.links: Dict[str, DriveLink] = {}
        self.writes: List[str] = []           # one entry per write: "create:<key>" / "consume:<key>"
        self.reads = 0                        # every peek / consume

    async def create(self, link: DriveLink) -> None:
        if link.link_key in self.links:
            raise AlreadyExists(f"link {link.link_key[:8]} exists")
        self.links[link.link_key] = link
        self.writes.append(f"create:{link.link_key}")

    async def peek(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        self.reads += 1
        link = self.links.get(link_key)
        return link if link is not None and link.live(now, max_reads) else None

    async def consume(self, link_key: str, now: datetime, max_reads: int) -> Optional[DriveLink]:
        self.reads += 1
        link = self.links.get(link_key)
        if link is None or not link.live(now, max_reads):
            return None
        self.links[link_key] = replace(link, reads=link.reads + 1, used_at=link.used_at or now)
        self.writes.append(f"consume:{link_key}")
        return link
