"""The drive fan-out's reconnect-notice world, per connection (MULTI_DRIVE_RFC §4.4, §8): the real
`DriveSyncSchedulerService` over the in-memory repository, the connections in `FakeDriveConnectionService` (its
`credential_gone` marks an ACTIVE connection whose credential is gone), a recording notifier whose `result` is scripted.
One connection `conn-1` of user `usr-1` (tag a1, indexing on), its sync state stored. Technical ids only."""
from typing import List, Optional

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.agent import AgentStatus
from src.domain.drive_index import DriveSyncState, ErrorCategory
from src.domain.notify_result import NotifyResult
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.support.drive_connection_service_fake import FakeDriveConnectionService, connection

from .drive_index_fakes import Clock, FakeQueue

USER, CONN = "usr-1", "conn-1"


class RecordingNotifier:
    """`result` is what notify returns; every call is recorded."""

    def __init__(self) -> None:
        self.calls: List[dict] = []
        self.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)

    async def notify(self, user_id, account_id, system_alert, *, kind, **_):
        self.calls.append({"user_id": user_id, "account_id": account_id, "alert": system_alert, "kind": kind})
        return self.result


class ReconnectWorld:
    def __init__(self) -> None:
        self.clock = Clock()
        self.repo = InMemoryDriveIndexRepository()
        self.queue = FakeQueue()
        self.connections = FakeDriveConnectionService()
        self.notifier = RecordingNotifier()
        self.svc = DriveSyncSchedulerService(self.repo, self.queue, self.connections.repo, self.notifier, None,
                                             connection_service=self.connections, clock=self.clock)

    async def connected(self, *, indexing: bool = True,
                        last_error: Optional[ErrorCategory] = None) -> None:
        """The connection and its sync state (a past index)."""
        await self.connections.add(connection(USER, CONN, indexing=indexing))
        await self.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id=CONN, created_at=self.clock.now,
                                                       last_error=last_error))

    async def state(self) -> Optional[DriveSyncState]:
        return self.repo.states.get(CONN)

    def credential_gone(self, gone: bool = True) -> None:
        (self.connections.credential_gone.add if gone else self.connections.credential_gone.discard)((USER, CONN))
