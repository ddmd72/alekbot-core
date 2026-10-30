"""The `start_drive_sync` fan-out per connection (MULTI_DRIVE_RFC §8, §4.4): one tick per ACTIVE connection with
indexing on and per stored sync state, payload `{user_id, connection_id}` and a key of the connection; a minimal state
only for an ACTIVE connection; the reconnect notice per connection, read from the connections (R1); the initial build of
the connection's provider account. Technical ids only."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import DriveSyncState
from src.domain.integration import Integration
from src.domain.provider_account import ProviderAccount
from src.domain.reconnect_policy import R1_NOTICES
from src.services.drive_sync_scheduler_service import RECONNECT_NOTICE, DriveSyncSchedulerService

from tests.support.drive_connection_service_fake import FakeDriveConnectionService, connection
from .drive_index_fakes import Clock, FakeQueue

USER = "usr-1"


class Notifier:
    def __init__(self) -> None:
        self.sent: list = []

    async def notify(self, *, user_id, account_id, system_alert, kind):
        self.sent.append((user_id, account_id, system_alert))
        return SimpleNamespace(delivered=True, sent=True, agent_status=SimpleNamespace(value="ok"), error=None,
                               text="sent")


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.repo = InMemoryDriveIndexRepository()
        self.queue = FakeQueue()
        self.connections = FakeDriveConnectionService()
        self.notifier = Notifier()
        self.accounts = {"cred-1": ProviderAccount(realm="realm-x", subject="sub-1", label="")}

        async def account_of(user_id, credential_id):
            return self.accounts.get(credential_id)

        self.svc = DriveSyncSchedulerService(self.repo, self.queue, self.connections.repo, self.notifier, None,
                                             connection_service=self.connections,
                                             clock=self.clock)


@pytest.fixture
def w():
    return World()


def _ticks(w):
    return sorted((t["payload"]["connection_id"], t["key"].split(":tick:")[0]) for t in w.queue.tasks)


async def test_one_tick_per_active_connection_with_indexing_on(w):
    await w.connections.add(connection(USER, "conn-1", ref_tag="a1"))
    await w.connections.add(connection(USER, "conn-2", ref_tag="b2"))
    await w.connections.add(connection(USER, "conn-3", ref_tag="c3", indexing=False))
    summary = await w.svc.fan_out()
    assert _ticks(w) == [("conn-1", "conn-1"), ("conn-2", "conn-2")]
    assert all(t["payload"] == {"user_id": USER, "connection_id": t["payload"]["connection_id"]} for t in w.queue.tasks)
    assert summary["enqueued"] == 2 and summary["candidates"] == 2
    # the minimal state per ticked connection: the staleness baseline (§12.1)
    assert {cid: (s.user_id, s.connection_id) for cid, s in w.repo.states.items()} == {
        "conn-1": (USER, "conn-1"), "conn-2": (USER, "conn-2")}


async def test_a_stored_state_is_visited_whatever_the_connection_so_its_index_gets_purged(w):
    await w.connections.add(connection(USER, "conn-1", indexing=False))
    await w.connections.add(connection(USER, "conn-2", ref_tag="b2"))
    await w.connections.disconnect(USER, "conn-2")
    for cid in ("conn-1", "conn-2"):
        await w.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id=cid))
    await w.svc.fan_out()
    assert _ticks(w) == [("conn-1", "conn-1"), ("conn-2", "conn-2")]


async def test_no_minimal_state_for_a_connection_that_needs_a_reconnect(w):
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.connections.flag_reconnect(conn.handle)
    await w.svc.fan_out()
    assert _ticks(w) == [] and w.repo.states == {}


async def test_a_fresh_continuation_skips_the_connection(w):
    await w.connections.add(connection(USER, "conn-1"))
    await w.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id="conn-1",
                                                continuation_enqueued_at=w.clock.now - timedelta(seconds=10)))
    summary = await w.svc.fan_out()
    assert summary["continuing"] == 1 and w.queue.tasks == []


async def test_the_reconnect_notice_goes_once_per_connection_and_its_flag_is_on_that_state(w):
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.connections.add(connection(USER, "conn-2", ref_tag="b2", credential_id="cred-2"))
    for cid in ("conn-1", "conn-2"):
        await w.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id=cid, created_at=w.clock.now))
    await w.connections.flag_reconnect(conn.handle)
    w.connections.credential_gone.add((USER, "conn-2"))   # R1: an ACTIVE connection whose credential is gone
    await w.svc.fan_out()
    assert sorted(text for _, _, text in w.notifier.sent) == sorted([RECONNECT_NOTICE, R1_NOTICES[Integration.DRIVE]])
    assert all(account == "acc-1" for _, account, _ in w.notifier.sent)
    assert w.repo.states["conn-1"].reconnect_notified and w.repo.states["conn-2"].reconnect_notified
    await w.svc.fan_out()
    assert len(w.notifier.sent) == 2   # once per lost grant
    # a good grant again lowers the flag of that connection only
    await w.connections.repo.transition(USER, "conn-1", ConnectionState.ACTIVE, w.clock.now)
    await w.svc.fan_out()
    assert not w.repo.states["conn-1"].reconnect_notified and w.repo.states["conn-2"].reconnect_notified


async def test_a_disconnected_connection_is_never_notified(w):
    await w.connections.add(connection(USER, "conn-1"))
    await w.repo.save_sync_state(DriveSyncState(user_id=USER, connection_id="conn-1", created_at=w.clock.now))
    await w.connections.disconnect(USER, "conn-1")
    w.connections.credential_gone.add((USER, "conn-1"))
    await w.svc.fan_out()
    assert w.notifier.sent == []


async def test_the_initial_build_read_is_the_connections_account(w):
    conn = await w.connections.add(connection(USER, "conn-1"))
    await w.repo.spend_initial_build(USER, "realm-x", "sub-1", 40.0, w.clock.now)
    assert (await w.svc._initial_build(conn)).spent_usd == 40.0
    w.accounts.clear()   # the credential is gone: no build counts, read as closed
    assert (await w.svc._initial_build(conn)).closed_at is not None


async def test_a_state_of_another_users_connection_id_is_never_read_as_this_one(w):
    await w.connections.add(connection(USER, "conn-1"))
    await w.repo.save_sync_state(DriveSyncState(user_id="usr-2", connection_id="conn-1", epoch=7))
    await w.svc.fan_out()
    # I3: the stored usr-2 state is absent for usr-1's conn-1, and never overwritten by a minimal state
    assert w.repo.states["conn-1"].user_id == "usr-2" and w.repo.states["conn-1"].epoch == 7
    assert await w.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id="conn-1")) is None
