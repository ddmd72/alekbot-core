"""DriveOperations and the mount tree (MULTI_DRIVE_RFC §11.1, slice 2a part P1): a path is `<mount>/<path>`, canonical in
everything printed and relative below; `/` lists the drives; only the primary can be changed; a move across drives is
refused; a held former name answers with its notice. Technical ids and the invented cast only."""
from tests.support.drive_access_stub import allow_access
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_connection_state import ConnectionState
from src.domain.drive_line import DriveLine
from src.domain.drive_mount_name_change import MountNameChange
from src.domain.drive_mounts import CROSS_DRIVE_LINE, RENAME_NOTICE
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search_line import SearchLine
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
CTX = {"account_id": "acc-1"}
PLAN = "Tom's plan.docx"


class StubSearch:
    """Stands in for DriveSearchService (part P2): records its calls, answers what the test sets."""

    def __init__(self) -> None:
        self.search_calls: list = []
        self.excerpt_calls: list = []
        self.states: Dict[str, SearchLine] = {}
        self.result: Any = None

    async def search(self, user_id, request, **kwargs):
        self.search_calls.append(kwargs)
        return self.result

    async def excerpts(self, user_id, request, **kwargs):
        self.excerpt_calls.append(kwargs)
        return self.result

    async def search_states(self, user_id):
        return self.states


async def _rename(world, connection, name, *, history=()):
    stored = replace(connection, mount_name=name, name_history=tuple(history))
    world.repo.connections[(stored.user_id, stored.connection_id)] = stored
    return stored


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    w.drive = FakeDriveAdapter("fake_a")
    w.connections = DriveConnectionService(w.repo, None, clock=lambda: w.now)
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await _rename(w, await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False,
                                        created_at=T1), "Acme")
    w.rd = w.drive.plant(w.one.handle, "root", "R&D", folder=True)
    w.plan = w.drive.plant(w.one.handle, w.rd, PLAN, b"plan")
    w.txt = w.drive.plant(w.one.handle, w.rd, "notes.txt", b"n")
    w.acme_rd = w.drive.plant(w.two.handle, "root", "R&D", folder=True)
    w.acme_plan = w.drive.plant(w.two.handle, w.acme_rd, "Hamburg.txt", b"hh")
    w.search = StubSearch()
    conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=w.drive,
                                       connections=w.connections, access=allow_access())
    w.service = UserDriveService(w.drive, w.connections)
    w.ops = DriveOperations(conversion_service=conversion, drive_service=w.service, drive_search=w.search)
    return w


def _names(world, connection):
    return {n.name for n in world.drive.tree(connection.handle).nodes.values()}


class TestPrintedPathsAreCanonical:
    async def test_a_listing_prints_the_mount_first(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", "Drive/R&D")
        assert out.text.startswith(f"[Drive folder: Drive/R&D/ ref=drive:a1:{world.rd}]")
        assert f"[Drive: Drive/R&D/{PLAN} (4B) ref=drive:a1:{world.plan}]" in out.text
        context = out.drive_context()
        assert context[0] == {"path": "Drive/R&D", "ref": f"drive:a1:{world.rd}"}
        assert {"path": f"Drive/R&D/{PLAN}", "ref": f"drive:a1:{world.plan}"} in context

    async def test_an_old_relative_path_lists_the_primarys_folder(self, world):
        old = await world.ops.list_folder(CTX, "usr-1", "R&D")
        new = await world.ops.list_folder(CTX, "usr-1", "Drive/R&D")
        assert old.success and old.text == new.text

    async def test_the_top_level_is_the_primarys_and_empty_stays_the_top_level(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", "")
        assert out.text.startswith("[Drive folder: Drive/ ref=drive:a1:root]")
        assert "Drive/R&D/" in out.text

    async def test_a_path_naming_another_mount_lists_that_drive(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", "Acme/R&D")
        assert out.success and f"[Drive: Acme/R&D/Hamburg.txt (2B) ref=drive:b2:{world.acme_plan}]" in out.text
        assert out.ref_tag == "b2"

    async def test_a_held_former_name_answers_with_its_notice_first(self, world):
        await _rename(world, world.two, "Acme Hamburg",
                      history=(MountNameChange("Acme", T1), MountNameChange("Acme Hamburg", world.now - timedelta(days=2))))
        out = await world.ops.list_folder(CTX, "usr-1", "Acme/R&D")
        assert out.text.splitlines()[0] == RENAME_NOTICE.format(former="Acme", current="Acme Hamburg")
        assert out.text.splitlines()[1].startswith("[Drive folder: Acme Hamburg/R&D/")

    async def test_an_open_prints_the_canonical_path_and_a_folder_is_refused_with_it(self, world):
        opened = await world.ops.open(CTX, "usr-1", f"drive:a1:{world.rd}")
        assert opened.cls is OutcomeClass.REQUEST_ERROR and opened.text == "'Drive/R&D' is a folder; list it instead."
        text = (await world.ops.open(CTX, "usr-1", f"drive:a1:{world.plan}")).text
        assert f"Drive/R&D/{PLAN}" in text.replace("&amp;", "&")

    async def test_a_service_refusal_names_the_canonical_path(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", "Drive/R&D/Missing")
        assert out.cls is OutcomeClass.NOT_FOUND and out.text == "No folder 'Drive/R&D/Missing' on the drive"


class TestTreeListing:
    async def test_slash_lists_the_drives_without_a_provider_call(self, world):
        out = await world.ops.list_folder(CTX, "usr-1", "/")
        assert out.success and out.cls is OutcomeClass.OK and out.items == () and out.drive_context() == []
        assert out.text.splitlines() == [
            "Drives (a path without a drive name is on the primary drive):",
            "- Drive: primary; connected; writable; searched by default",
            "- Acme: connected; read-only; searched only when named"]
        assert world.drive.calls == []

    async def test_a_mount_that_needs_a_reconnect_is_still_listed(self, world):
        world.repo.connections[("usr-1", "conn-2")] = replace(world.two, state=ConnectionState.NEEDS_RECONNECT)
        out = await world.ops.list_folder(CTX, "usr-1", "/")
        assert "- Acme: reconnect needed; read-only; searched only when named" in out.text

    async def test_no_connection_is_not_connected(self, world):
        out = await world.ops.list_folder(CTX, "usr-9", "/")
        assert out.cls is OutcomeClass.NOT_CONNECTED


class TestWrites:
    async def test_an_empty_folder_saves_to_the_primarys_inbox_and_says_so(self, world):
        out = await world.ops.save_text(CTX, "usr-1", "hi", file_name="note.md", folder="", wait_s=5)
        assert out.success and "Drive/Inbox/note.md" in out.text and "Created folders: Drive/Inbox" in out.text
        assert "note.md" in _names(world, world.one)

    async def test_a_prefixed_folder_saves_on_the_primary(self, world):
        out = await world.ops.save_text(CTX, "usr-1", "hi", file_name="note.md", folder="Drive/R&D", wait_s=5)
        assert f"Saved [Drive: Drive/R&D/note.md" in out.text

    async def test_a_new_folder_on_the_primary_prints_canonical_paths(self, world):
        out = await world.ops.create_folder(CTX, "usr-1", "Drive/Springfield/Maps", wait_s=5)
        assert "Drive/Springfield/Maps/" in out.text and "Created: Drive/Springfield, Drive/Springfield/Maps." in out.text

    async def test_a_move_to_another_drive_is_cross_drive_and_nothing_moves(self, world):
        out = await world.ops.move(CTX, "usr-1", f"drive:a1:{world.plan}", folder="Acme/R&D", wait_s=5)
        assert out.text == CROSS_DRIVE_LINE and not out.success
        assert world.drive.tree(world.one.handle).nodes[world.plan].parent_id == world.rd

    async def test_a_move_prints_canonical_before_and_after(self, world):
        out = await world.ops.move(CTX, "usr-1", f"drive:a1:{world.plan}", folder="Drive/Springfield", wait_s=5)
        assert out.text.startswith(f"Moved: Drive/R&D/{PLAN} → [Drive: Drive/Springfield/{PLAN}")
        assert "Created folders: Drive/Springfield." in out.text

    async def test_delete_and_append_print_canonical_paths(self, world):
        appended = await world.ops.append(CTX, "usr-1", f"drive:a1:{world.txt}", "more", wait_s=5)
        assert appended.text.startswith("Appended to [Drive: Drive/R&D/notes.txt")
        refused = await world.ops.append(CTX, "usr-1", f"drive:a1:{world.plan}", "more", wait_s=5)
        assert refused.text == f"'Drive/R&D/{PLAN}' is not a text file; text can only be appended to text files"
        deleted = await world.ops.delete(CTX, "usr-1", f"drive:a1:{world.rd}", wait_s=5)
        assert deleted.text == "Deleted folder Drive/R&D (2 files)."

    async def test_a_delete_receipt_carries_the_canonical_path(self, world):
        notifier = AsyncMock()
        localization = SimpleNamespace(get_ui_string=lambda lang, m: "DEL {path}")
        ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), notification=notifier,
                              drive_service=world.service, localization=localization)
        await ops.delete(CTX, "usr-1", f"drive:a1:{world.plan}", wait_s=5)
        assert notifier.notify_raw.await_args.args[2] == f"DEL Drive/R&D/{PLAN}"

    async def test_the_late_failure_notice_names_the_canonical_path(self, world):
        notifier = AsyncMock()
        localization = SimpleNamespace(get_ui_string=lambda lang, m: "FAIL {path}")
        ops = DriveOperations(conversion_service=AsyncMock(spec=FileConversionService), notification=notifier,
                              drive_service=world.service, localization=localization)
        from src.infrastructure.drive_operations import _LateNotice
        from src.domain.ui_messages import UIMessage
        await ops._late_failure_notice(
            _LateNotice(CTX, f"drive:a1:{world.plan}", "usr-1", UIMessage.DRIVE_DELETE_FAILED), None)
        assert notifier.notify_raw.await_args.args[2] == f"FAIL Drive/R&D/{PLAN}"


class TestReconnect:
    async def test_a_primary_that_needs_a_reconnect_answers_not_connected_for_a_path(self, world):
        world.repo.connections[("usr-1", "conn-1")] = replace(world.one, state=ConnectionState.NEEDS_RECONNECT)
        out = await world.ops.list_folder(CTX, "usr-1", "R&D")
        assert out.cls is OutcomeClass.NOT_CONNECTED and "not connected" in out.text

    async def test_a_named_mount_that_needs_a_reconnect_answers_the_same(self, world):
        world.repo.connections[("usr-1", "conn-2")] = replace(world.two, state=ConnectionState.NEEDS_RECONNECT)
        out = await world.ops.list_folder(CTX, "usr-1", "Acme/R&D")
        assert out.cls is OutcomeClass.NOT_CONNECTED and "not connected" in out.text
        assert world.drive.calls == []


class TestPreflightMounts:
    async def test_two_connections_give_two_lines_with_their_search_states(self, world):
        world.search.states = {"conn-1": SearchLine.ON, "conn-2": SearchLine.BUILDING}
        pre = await world.ops.preflight("usr-1")
        assert pre.drive is DriveLine.CONNECTED
        assert [(m.name, m.primary, m.search, m.writable) for m in pre.mounts] == [
            ("Drive", True, SearchLine.ON, True), ("Acme", False, SearchLine.BUILDING, False)]

    async def test_one_connection_gives_one_line_and_a_failed_read_leaves_the_search_part_out(self, world):
        async def broken(user_id):
            raise RuntimeError("down")
        world.search.search_states = broken
        world.repo.connections.pop(("usr-1", "conn-2"))
        pre = await world.ops.preflight("usr-1")
        assert [(m.name, m.search) for m in pre.mounts] == [("Drive", None)]

    async def test_no_connection_has_no_mount_lines(self, world):
        assert (await world.ops.preflight("usr-9")).mounts == ()


class TestDrivesAreHandedOn:
    async def test_the_refusal_of_the_search_service_is_a_request_error(self, world):
        line = 'There is no drive named "Acme Lviv". The drives are: Drive, Acme.'
        world.search.result = SimpleNamespace(status="drive_refused", refusal=line)
        out = await world.ops.search(CTX, "usr-1", query="plan", drives="Acme Lviv")
        assert out.cls is OutcomeClass.REQUEST_ERROR and out.text == line and not out.success
        assert world.search.search_calls == [{"drives": "Acme Lviv"}]
        passages = await world.ops.find_passages(CTX, "usr-1", question="what happens next", drives="Acme Lviv")
        assert passages.cls is OutcomeClass.REQUEST_ERROR and passages.text == line
        assert world.search.excerpt_calls == [{"drives": "Acme Lviv", "shown": frozenset()}]

    async def test_a_call_without_drives_is_todays_call(self, world):
        world.search.result = SimpleNamespace(status="drive_refused", refusal="x")
        await world.ops.search(CTX, "usr-1", query="plan")
        assert world.search.search_calls == [{}]
