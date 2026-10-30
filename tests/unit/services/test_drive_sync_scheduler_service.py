"""DriveSyncSchedulerService: fan-out, staleness alert, reconnect notice
(USER_DRIVE_INDEXING_RFC §7.1, §7.7, §12.1; RFC Δ5, Δ6)."""
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_connection import DriveConnection as Connection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, ErrorCategory, Priority
from src.domain.drive_index_params import STALE_ALERT_AFTER, TICK_DEADLINE_S
from src.domain.drive_task_keys import DRIVE_SYNC_TASK, tick_task_key
from src.domain.notification_kind import NotificationKind
from src.domain.notify_result import NotifyResult
from src.domain.agent import AgentStatus
from src.domain.user_drive import DriveConnection
from src.services.drive_sync_scheduler_service import RECONNECT_NOTICE, DriveSyncSchedulerService
from tests.unit.services.drive_index_fakes import T0, Clock, FakeDrive, FakeQueue, FakeUsers, legacy_account
from .drive_index_fakes import U_SCOPE


class FakeAlerts:
    def __init__(self, fail: bool = False) -> None:
        self.posts: List[str] = []
        self.fail = fail

    async def post(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("webhook down")
        self.posts.append(text)


class FakeNotifier:
    """`result` is what notify returns; `raises` makes it raise instead. `queue_sizes` records how many
    drive_sync tasks were already enqueued when each notice was attempted."""

    def __init__(self, queue: FakeQueue) -> None:
        self.calls: List[dict] = []
        self.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)
        self.raises: Optional[Exception] = None
        self.queue = queue
        self.queue_sizes: List[int] = []

    async def notify(self, user_id, account_id, system_alert, *, kind, **_):
        self.calls.append({"user_id": user_id, "account_id": account_id, "alert": system_alert, "kind": kind})
        self.queue_sizes.append(len(_drive_tasks(self.queue)))
        if self.raises is not None:
            raise self.raises
        return self.result


class PerUserDrive(FakeDrive):
    """FakeDrive with one connection state per user (the shared fake has a single global one)."""

    def __init__(self) -> None:
        super().__init__()
        self.per_user: Dict[str, DriveConnection] = {}

    async def connection(self, user_id: str) -> DriveConnection:
        return self.per_user.get(user_id, self.connection_state)


class LegacyConnectionRepository:
    """The scheduler's connection repository (MULTI_DRIVE_RFC §4.4, §8) over this file's single-drive flags: one
    connection per user in `connected` (a credential holder), whose id IS the user id (so every key reads as before),
    indexing from the user's `drive_indexing_enabled`, account from the profile, and state from the drive's per-user
    connection: connected and not flagged ACTIVE, flagged NEEDS_RECONNECT, not connected DISCONNECTED (a deliberate
    disconnect). A user with no profile (deleted) or no credential has no connection. Read live, as the flags were."""

    def __init__(self, users: FakeUsers, drive: "PerUserDrive", connected: List[str]) -> None:
        self.users = users
        self.drive = drive
        self.connected = connected

    async def get(self, user_id: str, connection_id: str) -> Optional[Connection]:
        if connection_id != user_id or user_id not in self.connected:
            return None
        profile = await self.users.get_user(user_id)
        if profile is None:
            return None
        legacy = await self.drive.connection(user_id)
        state = (ConnectionState.DISCONNECTED if not legacy.connected
                 else ConnectionState.NEEDS_RECONNECT if legacy.needs_reconnect else ConnectionState.ACTIVE)
        return Connection(connection_id=user_id, user_id=user_id, account_id=profile.account_id, credential_id="cred",
                          kind="fake", drive_identity="fake:d1", ref_tag="a1",
                          indexing_enabled=bool(profile.config.drive_indexing_enabled), state=state, created_at=T0,
                          disconnected_at=T0 if state is ConnectionState.DISCONNECTED else None,
                          provider_account=legacy_account(user_id))

    async def list_by_state(self, states, *, indexing_enabled: Optional[bool] = None) -> List[Connection]:
        found = [await self.get(u, u) for u in self.connected]
        return [c for c in found if c is not None and c.state in set(states)
                and (indexing_enabled is None or c.indexing_enabled is indexing_enabled)]


def scope(user_id: str) -> ConnectionScope:
    """The scope of a user's one connection in this environment (connection id = user id)."""
    return ConnectionScope(user_id=user_id, connection_id=user_id)


@pytest.fixture
def env():
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    drive = PerUserDrive()
    queue = FakeQueue()
    users = FakeUsers(enabled={"u": True})
    connected = ["u"]
    alerts = FakeAlerts()
    notifier = FakeNotifier(queue)

    connections = LegacyConnectionRepository(users, drive, connected)
    svc = DriveSyncSchedulerService(repo=repo, queue=queue, connections=connections, notifier=notifier,
                                    alert_sink=alerts, clock=clock)
    return SimpleNamespace(svc=svc, clock=clock, repo=repo, drive=drive, queue=queue, users=users,
                           connected=connected, alerts=alerts, notifier=notifier, connections=connections)


def _drive_tasks(queue: FakeQueue):
    return [t for t in queue.tasks if t["task_type"] == DRIVE_SYNC_TASK]


class TestFanOut:
    async def test_enabled_user_gets_one_tick_with_bucket_key_and_deadline(self, env):
        summary = await env.svc.fan_out()
        assert summary["enqueued"] == 1
        [task] = _drive_tasks(env.queue)
        assert task["payload"] == {"user_id": "u", "connection_id": "u"}
        assert task["key"] == tick_task_key(U_SCOPE, env.clock.now)
        assert task["deadline"] == TICK_DEADLINE_S

    async def test_second_fan_out_in_same_bucket_is_deduplicated(self, env):
        await env.svc.fan_out()
        await env.svc.fan_out()
        assert len(_drive_tasks(env.queue)) == 1   # Cloud Tasks reserves the name (FakeQueue too)

    async def test_disabled_user_without_state_is_skipped(self, env):
        """A connection with indexing off is not listed (only ACTIVE connections with indexing on are, MULTI_DRIVE_RFC
        §8); with no stored state it is no candidate at all."""
        env.users.enabled["u"] = False
        summary = await env.svc.fan_out()
        assert summary["candidates"] == 0 and _drive_tasks(env.queue) == []

    async def test_disabled_user_with_state_is_ticked_so_the_purge_runs(self, env):
        env.users.enabled["u"] = False
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
        await env.svc.fan_out()
        assert len(_drive_tasks(env.queue)) == 1

    async def test_state_without_credentials_is_still_visited(self, env):
        """Union of credential holders and stored states (§7.1): an orphaned index is purged."""
        env.users.enabled["gone"] = False   # "gone" holds no credentials, only a stored state
        await env.repo.save_sync_state(DriveSyncState(user_id="gone", connection_id="gone"))
        summary = await env.svc.fan_out()
        assert summary["candidates"] == 2
        assert {t["payload"]["user_id"] for t in _drive_tasks(env.queue)} == {"u", "gone"}

    async def test_deleted_user_with_state_is_ticked(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="ghost", connection_id="ghost"))
        await env.svc.fan_out()
        assert "ghost" in {t["payload"]["user_id"] for t in _drive_tasks(env.queue)}

    async def test_fresh_continuation_skips_the_user(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", continuation_enqueued_at=env.clock.now))
        env.clock.advance(TICK_DEADLINE_S - 1)
        summary = await env.svc.fan_out()
        assert summary["continuing"] == 1 and _drive_tasks(env.queue) == []

    async def test_old_continuation_no_longer_skips(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", continuation_enqueued_at=env.clock.now))
        env.clock.advance(TICK_DEADLINE_S)
        await env.svc.fan_out()
        assert len(_drive_tasks(env.queue)) == 1

    async def test_one_failing_user_does_not_stop_the_rest(self, env):
        env.connected.append("v")
        env.users.enabled["v"] = True
        original = env.repo.get_sync_state

        async def flaky(scope):   # the fan-out reads no user profile any more: a connection's own read fails
            if scope.user_id == "u":
                raise RuntimeError("firestore blip")
            return await original(scope)

        env.repo.get_sync_state = flaky
        summary = await env.svc.fan_out()
        assert summary["errors"] == 1
        assert [t["payload"]["user_id"] for t in _drive_tasks(env.queue)] == ["v"]

    async def test_fan_out_writes_only_the_two_flags(self, env):
        state = DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now, cursor="delta:7", epoch=3)
        await env.repo.save_sync_state(state)
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
        await env.svc.fan_out()
        after = await env.repo.get_sync_state(U_SCOPE)
        assert replace(after, stale_alerted=False, reconnect_notified=False) == state
        assert env.repo.records == {}


class TestStaleness:
    async def test_alert_on_transition_only(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now))
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
        await env.svc.fan_out()
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.alerts.posts) == 1 and "u" in env.alerts.posts[0]
        assert (await env.repo.get_sync_state(U_SCOPE)).stale_alerted is True

    async def test_flag_clears_when_fresh_again_and_next_incident_alerts(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now))
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
        await env.svc.fan_out()
        st = await env.repo.get_sync_state(U_SCOPE)
        await env.repo.save_sync_state(replace(st, last_success_at=env.clock.now))   # a clean tick
        await env.svc.fan_out()
        assert (await env.repo.get_sync_state(U_SCOPE)).stale_alerted is False
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
        await env.svc.fan_out()
        assert len(env.alerts.posts) == 2

    async def test_never_succeeded_user_alerts_after_stale_window(self, env):
        """A broken onboarding: every tick failed, so there is no last_success_at; created_at is the
        baseline (§12.1)."""
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", created_at=env.clock.now,
                                                      last_error=ErrorCategory.PROVIDER_ERROR))
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() - 60)
        await env.svc.fan_out()
        assert env.alerts.posts == []
        env.clock.advance(120)
        await env.svc.fan_out()
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.alerts.posts) == 1 and "never" in env.alerts.posts[0]

    async def test_overdue_class0_record_is_stale(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now))
        await env.repo.upsert_records([DriveIndexRecord(
            user_id="u", connection_id="u", item_id="a", due_at=env.clock.now - STALE_ALERT_AFTER - timedelta(seconds=1),
            priority=int(Priority.EDIT))])
        await env.svc.fan_out()
        assert len(env.alerts.posts) == 1

    async def test_overdue_cascade_is_not_stale(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now))
        await env.repo.upsert_records([DriveIndexRecord(
            user_id="u", connection_id="u", item_id="a", due_at=env.clock.now - STALE_ALERT_AFTER - timedelta(seconds=1),
            priority=int(Priority.CASCADE))])
        await env.svc.fan_out()
        assert env.alerts.posts == []

    async def test_access_lost_never_alerts(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now,
                                                      access_lost_at=env.clock.now))
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() * 3)
        await env.svc.fan_out()
        assert env.alerts.posts == []

    async def test_failed_alert_keeps_flag_down_and_still_enqueues(self, env):
        env.alerts.fail = True
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_success_at=env.clock.now,
                                                      last_error=ErrorCategory.INTERNAL))
        env.clock.advance(STALE_ALERT_AFTER.total_seconds() + 1)
        summary = await env.svc.fan_out()
        assert (await env.repo.get_sync_state(U_SCOPE)).stale_alerted is False
        assert summary["errors"] == 1 and len(_drive_tasks(env.queue)) == 1


class TestReconnectNotice:
    async def _lost(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u", last_error=ErrorCategory.RECONNECT_NEEDED))
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")

    async def test_reconnect_notice_once_per_breakage(self, env):
        await self._lost(env)
        await env.svc.fan_out()
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 1
        call = env.notifier.calls[0]
        assert call == {"user_id": "u", "account_id": "a", "alert": RECONNECT_NOTICE,
                        "kind": NotificationKind.DOCUMENT_DELIVERY}
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True

    async def test_good_grant_clears_flag_and_next_breakage_notifies_again(self, env):
        await self._lost(env)
        await env.svc.fan_out()
        env.drive.connection_state = DriveConnection(connected=True, drive_id="d1")
        env.clock.advance(600)
        await env.svc.fan_out()
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 2

    @pytest.mark.parametrize("error", ["no_channel", "channel_factory_returned_none:whatsapp"])
    async def test_nothing_sent_is_retried_next_fan_out(self, env, error):
        await self._lost(env)
        env.notifier.result = NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error=error)
        await env.svc.fan_out()
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
        env.notifier.result = NotifyResult(delivered=True, agent_status=AgentStatus.SUCCESS)
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 2
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True

    @pytest.mark.parametrize("result", [
        NotifyResult(delivered=False, agent_status=AgentStatus.FAILED, error="send raised", sent=True),
        NotifyResult(delivered=False, agent_status=AgentStatus.SUCCESS, error="rich content send failed", sent=True),
    ])
    async def test_attempt_that_may_have_reached_the_user_is_not_repeated(self, env, result):
        """A send that raised or a partial send (text out, rich content failed) sets the flag anyway:
        repeating it would message the user every 10 minutes. A formatter failure sends nothing and is
        retried (test_drive_reconnect_notice_retry.py)."""
        await self._lost(env)
        env.notifier.result = result
        await env.svc.fan_out()
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 1
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True

    async def test_notifier_raising_is_counted_and_retried(self, env):
        await self._lost(env)
        env.notifier.raises = RuntimeError("channel lookup failed")
        summary = await env.svc.fan_out()
        assert summary["errors"] == 1 and summary["enqueued"] == 1
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is False
        env.notifier.raises = None
        env.clock.advance(600)
        await env.svc.fan_out()
        assert len(env.notifier.calls) == 2
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True

    async def test_every_tick_is_enqueued_before_any_notice(self, env):
        """A notice runs through the formatter (up to 120 s): it must never delay another user's tick."""
        env.connected.append("v")
        env.users.enabled["v"] = True
        for user in ("u", "v"):
            await env.repo.save_sync_state(DriveSyncState(user_id=user, connection_id=user))
        env.drive.connection_state = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
        env.notifier.raises = RuntimeError("formatter down")
        summary = await env.svc.fan_out()
        assert env.notifier.queue_sizes == [2, 2]
        assert summary["enqueued"] == 2 and summary["errors"] == 2

    async def test_connection_states_are_per_user(self, env):
        env.connected.append("v")
        env.users.enabled["v"] = True
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
        await env.repo.save_sync_state(DriveSyncState(user_id="v", connection_id="v"))
        await env.repo.update_sync_flags(scope("v"), reconnect_notified=True)
        env.drive.per_user["u"] = DriveConnection(connected=True, needs_reconnect=True, drive_id="d1")
        env.drive.per_user["v"] = DriveConnection(connected=True, drive_id="d1")
        await env.svc.fan_out()
        assert [c["user_id"] for c in env.notifier.calls] == ["u"]
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True
        assert (await env.repo.get_sync_state(scope("v"))).reconnect_notified is False

    async def test_deliberate_disconnect_sends_no_notice_and_keeps_flag(self, env):
        await env.repo.save_sync_state(DriveSyncState(user_id="u", connection_id="u"))
        await env.repo.update_sync_flags(U_SCOPE, reconnect_notified=True)
        env.drive.connection_state = DriveConnection(connected=False)
        await env.svc.fan_out()
        assert env.notifier.calls == []
        assert (await env.repo.get_sync_state(U_SCOPE)).reconnect_notified is True

    async def test_disabled_user_gets_no_notice(self, env):
        await self._lost(env)
        env.users.enabled["u"] = False
        await env.svc.fan_out()
        assert env.notifier.calls == []
