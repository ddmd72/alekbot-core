"""UserDriveService by connection (MULTI_DRIVE_RFC §6, §8, §9; slice 1b part D): which connection a path and a ref act
on, the code-built line for a ref of no live connection, the nudge per connection, root and mirror guards per drive."""
from datetime import datetime, timezone

import pytest

from src.adapters.in_memory_drive_mirror_repo import InMemoryDriveMirrorRepository
from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_mirror import MirrorRoot
from src.domain.drive_ref_refusal import disconnected_ref_line, stale_ref_line
from src.domain.drive_task_keys import DRIVE_SYNC_TASK, nudge_task_key, parse_nudge, payload_scope
from src.domain.user_drive import (
    DriveMirrorWriteRefused, DriveNotConnectedError, DrivePathError, DriveRefRefused, DriveRootProtectedError,
)
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.drive_index_fakes import FakeQueue

ROOT = "root"
T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)


@pytest.fixture
def world():
    class World:
        repo = FakeDriveConnectionRepository()
        drive = FakeDriveAdapter("fake_a")
        queue = FakeQueue()
        mirror = InMemoryDriveMirrorRepository()
    w = World()
    w.service = UserDriveService(w.drive, connection_service(w.repo), nudge_queue=w.queue,
                                 mirror_roots=w.mirror)
    return w


async def _two(world):
    """usr-1 with conn-1 (primary, tag a1) and conn-2 (tag b2), the same item id on both on purpose (T-4)."""
    one = await seed(world.repo, connection_id="conn-1", tag="a1")
    two = await seed(world.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, created_at=T1)
    for connection, name, data in ((one, "on-one.txt", b"one"), (two, "on-two.txt", b"two")):
        world.drive.tree(connection.handle, drive_id=connection.connection_id)   # drive identity fake_a:<connection>
        assert world.drive.plant(connection.handle, ROOT, name, data) == "item-1"
    return one, two


def _names(world, handle):
    return {node.name for node in world.drive.tree(handle).nodes.values()}


class TestWhichConnection:
    async def test_a_tagged_ref_resolves_to_its_connection(self, world):
        _, two = await _two(world)
        resolved = await world.service.resolve_ref("usr-1", "[Drive: on-two.txt (3B) ref=drive:b2:item-1]")
        assert resolved.connection == two and resolved.item_id == "item-1"
        assert (await world.service.get_item(resolved.handle, "item-1")).name == "on-two.txt"

    async def test_an_untagged_ref_answers_search_again(self, world):
        await _two(world)
        with pytest.raises(DriveRefRefused) as refused:
            await world.service.resolve_ref("usr-1", "drive:item-1")
        assert refused.value.line == stale_ref_line("drive:item-1")

    async def test_a_tag_the_user_never_had_answers_search_again(self, world):
        await _two(world)
        with pytest.raises(DriveRefRefused) as refused:
            await world.service.resolve_ref("usr-1", "drive:zz:item-1")
        assert refused.value.line == stale_ref_line("drive:zz:item-1")

    async def test_a_disconnected_connections_tag_says_so(self, world):
        await seed(world.repo, connection_id="conn-9", tag="z9", state=ConnectionState.DISCONNECTED, primary=False)
        with pytest.raises(DriveRefRefused) as refused:
            await world.service.resolve_ref("usr-1", "drive:z9:item-1")
        assert refused.value.line == disconnected_ref_line("drive:z9:item-1")

    async def test_text_with_no_ref_is_a_path_error(self, world):
        with pytest.raises(DrivePathError):
            await world.service.resolve_ref("usr-1", "report.pdf")

    async def test_a_path_is_the_primarys_and_no_primary_is_not_connected(self, world):
        one, _ = await _two(world)
        assert await world.service.primary("usr-1") == one
        with pytest.raises(DriveNotConnectedError):
            await world.service.primary("usr-2")

    async def test_status_is_the_primarys(self, world):
        await seed(world.repo, state=ConnectionState.NEEDS_RECONNECT)
        status = await world.service.status("usr-1")
        assert status.connected and status.needs_reconnect


class TestOneConnectionOnly:
    async def test_a_save_lands_on_the_handles_connection_only(self, world):
        one, two = await _two(world)
        out = await world.service.save(two.handle, b"x", "new.txt", "text/plain", "Notes")
        assert out.item.path == "Notes/new.txt"
        assert _names(world, two.handle) >= {"Notes", "new.txt"}
        assert _names(world, one.handle) == {"", "on-one.txt"}

    async def test_a_delete_by_the_same_item_id_leaves_the_other_connection(self, world):
        one, two = await _two(world)
        await world.service.delete(two.handle, "item-1")
        assert (world.drive.tree(one.handle).recycle.keys(), world.drive.tree(two.handle).recycle.keys()) \
            == (set(), {"item-1"})
        assert (await world.service.get_item(one.handle, "item-1")).name == "on-one.txt"

    async def test_the_root_of_the_handles_drive_is_protected(self, world):
        one, _ = await _two(world)
        with pytest.raises(DriveRootProtectedError):
            await world.service.delete(one.handle, ROOT)


class TestNudgePerConnection:
    async def test_a_mutation_nudges_its_connection(self, world):
        _, two = await _two(world)
        await world.service.append_text(two.handle, "item-1", "more")
        [task] = world.queue.tasks
        assert task["task_type"] == DRIVE_SYNC_TASK
        assert payload_scope(task["payload"]) == two.scope
        nudge = parse_nudge(task["payload"])
        assert nudge is not None and nudge.item_ids == ("item-1",)
        assert task["key"] == nudge_task_key(two.scope, nudge.mutation_id, 0)


class TestMirrorGuardPerDrive:
    async def _root_on(self, world, handle, drive_identity: str) -> None:
        mirror = world.drive.plant(handle, ROOT, "_Transcripts", folder=True)
        await world.mirror.save_root(MirrorRoot(user_id="usr-1", drive_id="", root_item_id=mirror,
                                                root_name="_Transcripts", drive_identity=drive_identity))

    async def test_the_mirror_of_this_drive_is_refused(self, world):
        one, _ = await _two(world)
        await self._root_on(world, one.handle, "fake_a:conn-1")
        with pytest.raises(DriveMirrorWriteRefused):
            await world.service.save(one.handle, b"x", "a.md", "text/markdown", "_Transcripts")

    async def test_another_drives_mirror_does_not_guard_this_one(self, world):
        one, two = await _two(world)
        await self._root_on(world, one.handle, "fake_a:conn-1")
        world.drive.plant(two.handle, ROOT, "_Transcripts", folder=True)   # a plain folder of that name on conn-2
        out = await world.service.save(two.handle, b"x", "a.md", "text/markdown", "_Transcripts")
        assert out.item.path == "_Transcripts/a.md"

    async def test_a_root_stored_without_an_identity_guards_every_drive(self, world):
        _, two = await _two(world)
        await self._root_on(world, two.handle, "")
        with pytest.raises(DriveMirrorWriteRefused):
            await world.service.save(two.handle, b"x", "a.md", "text/markdown", "_Transcripts")
