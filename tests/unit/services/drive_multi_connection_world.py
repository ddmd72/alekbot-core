"""A world for the drive tick with several connections (MULTI_DRIVE_RFC §8): the real `DriveIndexingService` on the
in-memory index repository, one scripted drive per connection behind a router that dispatches on the handle (as the
composition's `DriveProviderRouter` does on its kind), the connection service fake with the service's rules, and the
index fakes. Technical ids only: usr-1, conn-1 (tag a1), conn-2 (tag b2)."""
from typing import Any, Dict, Optional

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection import DriveConnection
from src.domain.provider_account import ProviderAccount
from src.services.drive_indexing_service import DriveIndexingService, TickResult

from tests.support.drive_connection_service_fake import FakeDriveConnectionService, connection
from .drive_index_fakes import Clock, FakeDrive, FakeEmbeddings, FakeQueue, FakeUsers

USER = "usr-1"


class HandleRouter:
    """Forwards every port call to the drive of `handle.connection_id`, and records which connection each call was
    for (`calls`)."""

    def __init__(self, drives: Dict[str, FakeDrive]) -> None:
        self.drives = drives
        self.calls: list = []

    def __getattr__(self, name: str) -> Any:
        async def call(handle: Any, *args: Any, **kwargs: Any) -> Any:
            self.calls.append((name, handle.connection_id))
            return await getattr(self.drives[handle.connection_id], name)(handle, *args, **kwargs)
        return call


class MultiWorld:
    def __init__(self, notes: Any = None) -> None:
        self.clock = Clock()
        self.repo = InMemoryDriveIndexRepository()
        self.queue = FakeQueue()
        self.users = FakeUsers(enabled={USER: True})
        self.connections = FakeDriveConnectionService()
        self.drives: Dict[str, FakeDrive] = {}
        self.router = HandleRouter(self.drives)
        self.accounts: Dict[str, ProviderAccount] = {}   # credential id -> its provider account

        async def account_of(user_id: str, credential_id: str) -> Optional[ProviderAccount]:
            return self.accounts.get(credential_id)

        self.svc = DriveIndexingService(self.router, self.repo, FakeEmbeddings(), self.queue, self.users,
                                        lambda lat, lon: None, "test-space/8", connections=self.connections,
                                        account_of=account_of, notes=notes, clock=self.clock,
                                        monotonic=self.clock.monotonic)

    async def connect(self, connection_id: str = "conn-1", *, ref_tag: str = "a1",
                      drive_identity: str = "fake_a:drive-1", **kw: Any) -> DriveConnection:
        conn = await self.connections.add(connection(USER, connection_id, ref_tag=ref_tag,
                                                     drive_identity=drive_identity,
                                                     credential_id=f"cred-{connection_id}", **kw))
        self.drives[connection_id] = FakeDrive(drive_identity=drive_identity)
        self.accounts[f"cred-{connection_id}"] = ProviderAccount(realm="realm-x", subject=f"sub-{connection_id}",
                                                                 label="")
        return conn

    @staticmethod
    def scope(connection_id: str = "conn-1") -> ConnectionScope:
        return ConnectionScope(user_id=USER, connection_id=connection_id)

    async def tick(self, connection_id: str = "conn-1") -> TickResult:
        return await self.svc.tick(self.scope(connection_id))

    async def settle(self, connection_id: str = "conn-1", max_ticks: int = 30) -> TickResult:
        """Ticks while each one enqueues a continuation of this connection, as the drive queue would."""
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            result = await self.tick(connection_id)
            continued = [t for t in self.queue.tasks[before:]
                         if t["payload"].get("connection_id") == connection_id and ":cont:" in (t["key"] or "")]
            if not continued:
                return result
            self.clock.advance(max(1, max(t["delay"] for t in continued)))
        raise AssertionError("did not settle")

    def records(self, connection_id: str) -> Dict[str, Any]:
        return {item: rec for (cid, item), rec in self.repo.records.items() if cid == connection_id}
