"""DriveOperations by connection (MULTI_DRIVE_RFC §6, §9; slice 1b part D): a ref acts on its tag's connection, a path
and a save on the primary, every label and `drive_context` ref carries the connection's tag, and a ref of no live
connection answers its code-built line once, as not found, with nothing done."""
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_line import DriveLine
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_ref_refusal import disconnected_ref_line, stale_ref_line
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
CTX = {"account_id": "acc-1"}


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.drive = FakeDriveAdapter("fake_a")
    connections = connection_service(w.repo)
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, created_at=T1)
    assert w.drive.plant(w.one.handle, "root", "on-one.txt", b"one") == "item-1"
    assert w.drive.plant(w.two.handle, "root", "on-two.txt", b"two") == "item-1"     # the same item id on purpose (T-4)
    w.folder = w.drive.plant(w.two.handle, "root", "Work", folder=True)               # generated: item-2
    conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
                                       connections=connections)
    w.ops = DriveOperations(conversion_service=conversion, drive_service=UserDriveService(w.drive, connections))
    return w


def _names(world, connection):
    return {node.name for node in world.drive.tree(connection.handle).nodes.values()}


def _recycled(world):
    """The recycle bins of conn-1 and conn-2 (what a delete removed on each)."""
    return (set(world.drive.tree(world.one.handle).recycle), set(world.drive.tree(world.two.handle).recycle))


class TestRefsActOnTheirConnection:
    async def test_open_reads_the_tags_connection_and_tags_the_label(self, world):
        out = await world.ops.open(CTX, "usr-1", "drive:b2:item-1")
        assert out.success and "two" in out.text
        assert "ref=drive:b2:item-1" in out.text
        assert out.drive_context() == [{"path": "on-two.txt", "ref": "drive:b2:item-1"}]

    async def test_list_by_folder_ref_lists_that_connection(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", f"drive:b2:{world.folder}")
        assert out.success and out.text.startswith(f"[Drive folder: Work/ ref=drive:b2:{world.folder}]")

    async def test_a_delete_by_ref_deletes_on_that_connection_only(self, world):
        out = await world.ops.delete(CTX, "usr-1", "drive:b2:item-1", wait_s=5)
        assert out.success
        assert _recycled(world) == (set(), {"item-1"})

    async def test_a_move_answers_with_the_tagged_label(self, world):
        out = await world.ops.move(CTX, "usr-1", "drive:b2:item-1", folder="Work", wait_s=5)
        assert out.success and "ref=drive:b2:item-1" in out.text and out.ref_tag == "b2"


class TestPathsAreThePrimarys:
    async def test_a_listing_by_path_tags_the_primary(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", None)
        assert "[Drive: on-one.txt (3B) ref=drive:a1:item-1]" in out.text
        assert {e["ref"] for e in out.drive_context()} >= {"drive:a1:item-1"}

    async def test_a_saved_text_goes_to_the_primary(self, world):
        out = await world.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", folder="Notes", wait_s=5)
        assert out.success and "ref=drive:a1:" in out.text
        assert "note.md" in _names(world, world.one)
        assert "note.md" not in _names(world, world.two)

    async def test_no_primary_is_not_connected(self, world):
        await world.repo.save_settings(type(await world.repo.get_settings("usr-1"))(user_id="usr-1"))
        out = await world.ops.create_folder(CTX, "usr-1", "Work", wait_s=5)
        assert not out.success and out.cls is OutcomeClass.NOT_CONNECTED


class TestRefsOfNoConnection:
    @pytest.mark.parametrize("call", ["open", "move", "append", "delete"])
    async def test_an_untagged_ref_answers_search_again_and_does_nothing(self, world, call):
        ref = "drive:item-1"
        if call == "open":
            out = await world.ops.open(CTX, "usr-1", ref)
        elif call == "move":
            out = await world.ops.move(CTX, "usr-1", ref, new_name="x.txt", wait_s=5)
        elif call == "append":
            out = await world.ops.append(CTX, "usr-1", ref, "more", wait_s=5)
        else:
            out = await world.ops.delete(CTX, "usr-1", ref, wait_s=5)
        assert not out.success and out.cls is OutcomeClass.NOT_FOUND
        assert out.text == stale_ref_line(ref)
        assert _recycled(world) == (set(), set())
        assert world.drive.tree(world.one.handle).nodes["item-1"].data == b"one"

    async def test_a_disconnected_connections_ref_says_so(self, world):
        await seed(world.repo, connection_id="conn-9", tag="z9", state=ConnectionState.DISCONNECTED, primary=False)
        out = await world.ops.open(CTX, "usr-1", "drive:z9:item-1")
        assert out.text == disconnected_ref_line("drive:z9:item-1") and out.cls is OutcomeClass.NOT_FOUND


class TestPreflight:
    async def test_the_drive_line_reads_the_primarys_status(self, world):
        assert (await world.ops.preflight("usr-1")).drive is DriveLine.CONNECTED
        assert (await world.ops.preflight("usr-2")).drive is DriveLine.NOT_CONNECTED

    async def test_a_folder_ref_becomes_its_path(self, world):
        assert await world.ops.folder_path("usr-1", f"drive:b2:{world.folder}") == "Work"
        assert await world.ops.folder_path("usr-1", "drive:item-1") == "drive:item-1"   # stale: as given
