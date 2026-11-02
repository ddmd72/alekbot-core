"""T-12, the service half (MULTI_DRIVE_RFC §11.8, §9; design §1.6): `DriveConnectionService.rename` keeps the tag (every
`drive:b2:…` ref still resolves), the mount tree answers to the former name for `HOLD` with the notice, another
connection cannot take a held name, the shadowing warning, a DISCONNECTED connection, and two concurrent renames to one
name (exactly one wins). Also `default_name`."""
import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.drive_connection import DriveConnection
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import HOLD, NAME_HELD, NAME_TAKEN, RENAME_NOTICE, SHADOW_WARNING, resolve_path
from src.domain.drive_resolved_ref import ResolvedDriveRef
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveNotConnectedError
from src.services.drive_connection_service import DriveConnectionService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
T_RENAME = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def conn(connection_id, name, tag, *, minutes=0, state=ConnectionState.ACTIVE):
    return DriveConnection(connection_id=connection_id, user_id="usr-1", account_id="acc-1",
                           credential_id=f"cred-{connection_id}", kind="fake_a",
                           drive_identity=f"fake_a:{connection_id}", ref_tag=tag, mount_name=name,
                           created_at=T0 + timedelta(minutes=minutes), state=state,
                           disconnected_at=T0 if state is ConnectionState.DISCONNECTED else None,
                           name_history=(MountNameChange(name, T0 + timedelta(minutes=minutes)),))


async def world(*connections):
    repo = FakeDriveConnectionRepository()
    for c in connections or (conn("conn-1", "Drive", "a1"), conn("conn-2", "Acme", "b2", minutes=1)):
        await repo.create(c)
    await repo.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
    clock = Clock(T_RENAME)
    return repo, DriveConnectionService(repo, None, clock=clock), clock


async def test_a_rename_keeps_the_tag_and_every_ref_resolving():
    repo, svc, _ = await world()
    result = await svc.rename("usr-1", "conn-2", "Acme Hamburg")
    assert result.ok and result.name == "Acme Hamburg" and result.warning == ""
    stored = await repo.get("usr-1", "conn-2")
    assert (stored.mount_name, stored.ref_tag) == ("Acme Hamburg", "b2")
    resolved = await svc.resolve_ref("usr-1", "drive:b2:item-7")
    assert isinstance(resolved, ResolvedDriveRef)
    assert (resolved.connection.connection_id, resolved.item_id) == ("conn-2", "item-7")


async def test_the_former_name_resolves_with_the_notice_until_the_hold_ends():
    _, svc, clock = await world()
    await svc.rename("usr-1", "conn-2", "Acme Hamburg")
    clock.now = T_RENAME + HOLD - timedelta(days=1)
    target = resolve_path(await svc.mounts("usr-1"), "Acme/R&D")
    assert (target.mount.connection_id, target.path) == ("conn-2", "R&D")
    assert target.notice == RENAME_NOTICE.format(former="Acme", current="Acme Hamburg") == \
        "Acme is now called Acme Hamburg."
    clock.now = T_RENAME + HOLD
    late = resolve_path(await svc.mounts("usr-1"), "Acme/R&D")
    assert (late.mount.connection_id, late.path, late.notice) == ("conn-1", "Acme/R&D", "")


async def test_another_drive_cannot_take_a_held_name():
    _, svc, clock = await world()
    await svc.rename("usr-1", "conn-2", "Acme Hamburg")
    clock.now = T_RENAME + timedelta(days=1)
    held = await svc.rename("usr-1", "conn-1", "Acme")
    assert held.error == NAME_HELD.format(name="Acme")
    clock.now = T_RENAME + HOLD
    assert (await svc.rename("usr-1", "conn-1", "Acme")).ok


async def test_a_name_another_drive_has_is_refused_and_nothing_is_written():
    repo, svc, _ = await world()
    result = await svc.rename("usr-1", "conn-2", "drive")
    assert result.error == NAME_TAKEN.format(name="drive")
    assert (await repo.get("usr-1", "conn-2")).mount_name == "Acme"


async def test_the_shadowing_warning_names_the_primary():
    repo, svc, _ = await world()
    result = await svc.rename("usr-1", "conn-2", "Springfield", primary_top_folders=["Springfield", "R&D"])
    assert result.ok and result.warning == SHADOW_WARNING.format(name="Springfield", primary="Drive")
    assert result.warning == "The folder Springfield in Drive will be reached as Drive/Springfield."
    assert (await repo.get("usr-1", "conn-2")).mount_name == "Springfield"


async def test_a_disconnected_or_absent_connection_is_not_renamed():
    repo, svc, _ = await world(conn("conn-1", "Drive", "a1"),
                               conn("conn-2", "Acme", "b2", minutes=1, state=ConnectionState.DISCONNECTED))
    with pytest.raises(DriveNotConnectedError):
        await svc.rename("usr-1", "conn-2", "Acme Hamburg")
    with pytest.raises(DriveNotConnectedError):
        await svc.rename("usr-1", "conn-9", "Acme Hamburg")
    assert (await repo.get("usr-1", "conn-2")).mount_name == "Acme"


async def test_two_concurrent_renames_to_one_name_exactly_one_wins():
    repo, svc, _ = await world()
    first, second = await asyncio.gather(svc.rename("usr-1", "conn-1", "Acme Hamburg"),
                                         svc.rename("usr-1", "conn-2", "Acme Hamburg"))
    assert sorted([first.ok, second.ok]) == [False, True]
    loser = first if not first.ok else second
    assert loser.error == NAME_TAKEN.format(name="Acme Hamburg")
    names = [c.mount_name for c in await repo.list_for_user("usr-1")]
    assert names.count("Acme Hamburg") == 1


async def test_default_name_is_the_lowest_free():
    _, svc, _ = await world()
    assert await svc.default_name("usr-1") == "Drive 2"
    _, empty, _ = await world(conn("conn-2", "Acme", "b2"))
    assert await empty.default_name("usr-1") == "Drive"
