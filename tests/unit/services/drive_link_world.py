"""A one-time link world for the slice-4 tests (DRIVE_MCP_CONNECTOR_RFC §8): the production `DriveLinkService`,
`UserDriveService` and `DriveConnectionService` (in-memory repository) over `FakeDriveAdapter`, the fake link store, a
recording `MediaStreamPort`, an injected clock and a grant check reading a dict.

Alex Doe's drive (connection `conn-1`, tag `a1`), connector access `R&D` + `12 Oak Street`:
`12 Oak Street/FA-0137.pdf`, `12 Oak Street/12 Oak Street.png`, `R&D/Tom's plan.docx`, `R&D/standup.m4a`.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import AsyncIterator, Dict, List, Optional, Tuple

from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_media_source import MediaSource
from src.domain.exceptions import MediaStreamFailed
from src.domain.user_drive import DriveItemNotFoundError
from src.ports.media_stream_port import MediaStreamPort
from src.services.drive_link_service import DriveLinkService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.drive_link_store import FakeDriveLinkStore
from tests.support.fake_drive_adapter import FakeDriveAdapter

USER = "usr-alex"
ACCOUNT = "acc-1"
CONN = "conn-1"
TAG = "a1"
GRANT = "grant-alex-000000000000"
BASE = "https://mcp.example.test"
T0 = datetime(2026, 11, 3, 9, 0, tzinfo=timezone.utc)
DIGEST_KEY = bytes(range(32))
INVOICE_BYTES = b"%PDF-1.7 invoice FA-0137 " * 4000           # ~100 KB: several chunks
PLAN_BYTES = b"PK docx Tom's plan"
IMAGE_BYTES = b"\x89PNG 12 Oak Street"


class RecordingMedia(MediaStreamPort):
    """Streams the bytes planted for a source URL in `chunk` pieces; records every stream it was asked for, and whether
    each was closed. `fail` makes the next stream raise before its first byte."""

    def __init__(self, chunk: int = 16 * 1024) -> None:
        self.bodies: Dict[str, bytes] = {}
        self.calls: List[str] = []
        self.closed: List[str] = []
        self.fail: Optional[Exception] = None
        self._chunk = chunk

    async def chunks(self, source: MediaSource) -> AsyncIterator[bytes]:
        self.calls.append(source.url)
        try:
            if self.fail is not None:
                failure, self.fail = self.fail, None
                raise failure
            body = self.bodies[source.url]
            for start in range(0, len(body), self._chunk):
                yield body[start:start + self._chunk]
        finally:
            self.closed.append(source.url)


class LinkWorld:
    def __init__(self) -> None:
        self.adapter = FakeDriveAdapter()
        self.conns = FakeDriveConnectionRepository()
        self.connections = connection_service(self.conns)
        self.drive = UserDriveService(self.adapter, self.connections)
        self.store = FakeDriveLinkStore()
        self.media = RecordingMedia()
        self.grants: Dict[Tuple[str, Optional[str]], bool] = {(USER, GRANT): True}
        self.now = T0
        self.ids = iter(f"{n:022d}".replace("0", "A") for n in range(1, 1000))

    async def grant_allowed(self, user_id: str, grant_id: Optional[str]) -> bool:
        return self.grants.get((user_id, grant_id), False)

    def service(self, max_reads: int = 1) -> DriveLinkService:
        return DriveLinkService(self.store, self.drive, self.media, grant_allowed=self.grant_allowed,
                                digest_key=DIGEST_KEY, base_url=BASE, clock=lambda: self.now,
                                new_id=lambda: next(self.ids), max_reads=max_reads)

    async def open(self) -> "LinkWorld":
        self.connection = await seed(self.conns, USER, CONN, tag=TAG, account_id=ACCOUNT)
        self.handle = self.connection.handle
        root = "root"
        self.rnd = self.adapter.plant(self.handle, root, "R&D", folder=True)
        self.oak = self.adapter.plant(self.handle, root, "12 Oak Street", folder=True)
        self.invoice = self.adapter.plant(self.handle, self.oak, "FA-0137.pdf", INVOICE_BYTES, mime="application/pdf")
        self.image = self.adapter.plant(self.handle, self.oak, "12 Oak Street.png", IMAGE_BYTES, mime="image/png")
        self.plan = self.adapter.plant(self.handle, self.rnd, "Tom's plan.docx", PLAN_BYTES)
        await self.share(ConnectorAccess.folders((self.rnd, self.oak)))
        for item, body in ((self.invoice, INVOICE_BYTES), (self.image, IMAGE_BYTES), (self.plan, PLAN_BYTES)):
            self.media.bodies[(await self.adapter.media_source(self.handle, item)).url] = body
        return self

    async def share(self, access: Optional[ConnectorAccess]) -> None:
        await self.conns.update_connector_access(USER, CONN, access)

    def ref(self, item_id: str) -> str:
        return f"drive:{TAG}:{item_id}"

    def later(self, seconds: float) -> None:
        self.now = T0 + timedelta(seconds=seconds)


async def drain(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


__all__ = ["LinkWorld", "RecordingMedia", "drain", "MediaStreamFailed", "DriveItemNotFoundError"]
