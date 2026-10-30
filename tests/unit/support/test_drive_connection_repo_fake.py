"""The in-memory DriveConnectionRepository (tests/support/drive_connection_repo.py) keeps the port's rules, so parts can
test against it: create never overwrites, update is partial and never moves the state, transition follows
`can_transition` atomically, the orders the port promises, settings read back whole."""
import asyncio
from datetime import datetime, timedelta, timezone

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_user_settings import DriveUserSettings
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

T0 = datetime(2026, 10, 30, 12, 0, tzinfo=timezone.utc)
ACTIVE, NEEDS, GONE = ConnectionState.ACTIVE, ConnectionState.NEEDS_RECONNECT, ConnectionState.DISCONNECTED


def _conn(user_id, connection_id, minutes=0, **over):
    base = dict(connection_id=connection_id, user_id=user_id, account_id="acc-1", credential_id=f"cred-{connection_id}",
                kind="fake_a", drive_identity=f"fake_a:{connection_id}", ref_tag="a1",
                created_at=T0 + timedelta(minutes=minutes))
    base.update(over)
    return DriveConnection(**base)


async def test_create_never_overwrites():
    repo = FakeDriveConnectionRepository()
    assert await repo.create(_conn("usr-1", "conn-1")) is True
    assert await repo.create(_conn("usr-1", "conn-1", mount_name="Other")) is False
    assert (await repo.get("usr-1", "conn-1")).mount_name == "Drive"
    assert await repo.get("usr-2", "conn-1") is None


async def test_list_for_user_is_oldest_first_every_state():
    repo = FakeDriveConnectionRepository()
    await repo.create(_conn("usr-1", "conn-2", minutes=5))
    await repo.create(_conn("usr-1", "conn-1", minutes=1, state=GONE, disconnected_at=T0))
    await repo.create(_conn("usr-2", "conn-3"))
    assert [c.connection_id for c in await repo.list_for_user("usr-1")] == ["conn-1", "conn-2"]


async def test_list_by_state_across_users_with_the_indexing_filter():
    repo = FakeDriveConnectionRepository()
    await repo.create(_conn("usr-2", "conn-4", indexing_enabled=True))
    await repo.create(_conn("usr-1", "conn-1", minutes=2, indexing_enabled=True))
    await repo.create(_conn("usr-1", "conn-2", minutes=1, indexing_enabled=False))
    await repo.create(_conn("usr-1", "conn-3", state=NEEDS, indexing_enabled=True))
    assert [c.connection_id for c in await repo.list_by_state([ACTIVE])] == ["conn-2", "conn-1", "conn-4"]
    assert [c.connection_id for c in await repo.list_by_state([ACTIVE], indexing_enabled=True)] == ["conn-1", "conn-4"]
    assert [c.connection_id for c in await repo.list_by_state([ACTIVE, NEEDS], indexing_enabled=True)] == [
        "conn-3", "conn-1", "conn-4"]


async def test_update_is_partial_and_never_moves_the_state():
    repo = FakeDriveConnectionRepository()
    await repo.create(_conn("usr-1", "conn-1", state=NEEDS))
    got = await repo.update("usr-1", "conn-1", indexing_enabled=True, credential_id="cred-9")
    assert (got.indexing_enabled, got.credential_id, got.transcribe_enabled, got.state) == (True, "cred-9", False, NEEDS)
    assert await repo.get("usr-1", "conn-1") == got
    assert await repo.update("usr-1", "conn-404", indexing_enabled=True) is None


async def test_transition_follows_the_rule_and_reports_the_edge():
    repo = FakeDriveConnectionRepository()
    await repo.create(_conn("usr-1", "conn-1"))
    assert await repo.transition("usr-1", "conn-1", NEEDS, T0) is True
    assert await repo.transition("usr-1", "conn-1", NEEDS, T0) is False          # already held
    assert await repo.transition("usr-1", "conn-1", GONE, T0) is True
    stored = await repo.get("usr-1", "conn-1")
    assert (stored.state, stored.disconnected_at) == (GONE, T0)
    assert await repo.transition("usr-1", "conn-1", ACTIVE, T0) is False         # terminal
    assert await repo.transition("usr-1", "conn-404", NEEDS, T0) is False


async def test_concurrent_flags_report_one_transition():
    repo = FakeDriveConnectionRepository()
    await repo.create(_conn("usr-1", "conn-1"))
    results = await asyncio.gather(*(repo.transition("usr-1", "conn-1", NEEDS, T0) for _ in range(5)))
    assert sorted(results) == [False, False, False, False, True]


async def test_settings_round_trip():
    repo = FakeDriveConnectionRepository()
    assert await repo.get_settings("usr-1") is None
    await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
    await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-2"))
    assert await repo.get_settings("usr-1") == DriveUserSettings(user_id="usr-1", primary_connection_id="conn-2")
