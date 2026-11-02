"""The access policy in front of every provider call of `UserDriveService` (MULTI_DRIVE_RFC §11.3, slice 2b P-B): one
test group per row of the design's call-site table. A refused call makes no provider call that reads or changes the
refused place, raises the policy's exact line, and the line never prints the path of what it refused (N3). Over
`FakeDriveAdapter` and the in-memory connection repository."""
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_access_policy import (
    NO_WRITE_FOLDER_LINE, OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE, PROTECTED_LINE,
)
from src.domain.drive_connection import DriveConnection
from src.domain.user_drive import DEFAULT_INBOX_FOLDER, DriveAccessRefused
from src.services.drive_connection_service import DriveConnectionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
MUTATING = {"create_folder", "upload", "replace_content", "move", "delete"}
OUT_SCOPE = OUTSIDE_SCOPE_LINE.format(drive="Acme")
OUT_WRITE = OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")


class World:
    def __init__(self, **settings):
        self.fake = FakeDriveAdapter("fake_b")
        self.repo = FakeDriveConnectionRepository()
        self.connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0, **settings)
        self.repo.connections[("usr-1", "conn-2")] = self.connection
        self.handle = self.connection.handle
        self.service = UserDriveService(self.fake, DriveConnectionService(self.repo, None))
        self.root = self.fake.tree(self.handle).root.item_id
        self.rd = self.fake.plant(self.handle, self.root, "R&D", folder=True)
        self.plan = self.fake.plant(self.handle, self.rd, "Tom's plan.docx", b"plan")
        self.coimbra = self.fake.plant(self.handle, self.root, "Coimbra 2024", folder=True)
        self.lab = self.fake.plant(self.handle, self.coimbra, "lab-report.pdf", b"lab")
        self.notes = self.fake.plant(self.handle, self.coimbra, "notes.txt", b"n")
        self.fake.calls.clear()

    def settings(self, **changes):
        self.repo.connections[("usr-1", "conn-2")] = replace(self.connection, **changes)

    def made(self):
        return [m for m, _ in self.fake.calls if m in MUTATING]

    def names(self):
        return {n.name for n in self.fake.tree(self.handle).nodes.values()}


async def refused(coro, line):
    with pytest.raises(DriveAccessRefused) as caught:
        await coro
    assert caught.value.line == line
    assert "Coimbra" not in line and "lab-report" not in line
    return caught.value


class TestReadOnlyDrive:
    """No write folder (D11): every change answers the line, and nothing is read or changed first."""

    @pytest.fixture
    def w(self):
        return World(write_folder_id=None)

    async def test_save_refuses_before_any_provider_call(self, w):
        await refused(w.service.save(w.handle, b"x", "a.txt", "text/plain", "R&D"), NO_WRITE_FOLDER_LINE)
        assert "list_children" not in [m for m, _ in w.fake.calls] and w.made() == []

    async def test_save_without_folder_refuses(self, w):
        await refused(w.service.save(w.handle, b"x", "a.txt", "text/plain", None), NO_WRITE_FOLDER_LINE)
        assert w.made() == []

    async def test_ensure_folder_refuses(self, w):
        await refused(w.service.ensure_folder(w.handle, "R&D/New"), NO_WRITE_FOLDER_LINE)
        assert w.made() == []

    @pytest.mark.parametrize("folder,new_name", [("R&D", None), (None, "renamed.docx")])
    async def test_move_and_rename_refuse(self, w, folder, new_name):
        await refused(w.service.move(w.handle, w.plan, folder, new_name), NO_WRITE_FOLDER_LINE)
        assert w.made() == []

    async def test_append_replace_delete_refuse(self, w):
        await refused(w.service.append_text(w.handle, w.notes, "more"), NO_WRITE_FOLDER_LINE)
        await refused(w.service.replace_with(w.handle, w.notes, b"new", "text/plain"), NO_WRITE_FOLDER_LINE)
        await refused(w.service.delete(w.handle, w.notes), NO_WRITE_FOLDER_LINE)
        assert w.made() == []

    async def test_reads_still_work(self, w):
        assert (await w.service.list_folder(w.handle, "R&D")).items[0].name == "Tom's plan.docx"
        assert (await w.service.get_item(w.handle, w.plan)).name == "Tom's plan.docx"


class TestAppFolderDefault:
    """The default (whole area readable, root write folder) behaves as before and walks no chain (G3)."""

    async def test_every_call_runs_with_zero_get_change(self):
        w = World()
        out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", "R&D/New")
        await w.service.append_text(w.handle, w.notes, "more")
        await w.service.replace_with(w.handle, w.notes, b"new", "text/plain")
        await w.service.move(w.handle, w.lab, "R&D", None)
        await w.service.list_folder(w.handle, "Coimbra 2024")
        await w.service.delete(w.handle, w.notes)
        assert out.item.name == "a.txt"
        assert [m for m, _ in w.fake.calls if m == "get_change"] == []


class TestWriteFolder:
    @pytest.fixture
    def w(self):
        w = World()
        w.settings(write_folder_id=w.rd)
        return w

    async def test_save_inside_lands(self, w):
        out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", "R&D")
        assert out.item.name == "a.txt" and w.made() == ["upload"]

    async def test_save_outside_is_refused_and_creates_nothing(self, w):
        await refused(w.service.save(w.handle, b"x", "a.txt", "text/plain", "Coimbra 2024/New"), OUT_WRITE)
        assert w.made() == [] and "New" not in w.names()

    async def test_resolve_create_outside_creates_nothing(self, w):
        await refused(w.service.ensure_folder(w.handle, "Springfield/Deep"), OUT_WRITE)
        assert w.made() == [] and "Springfield" not in w.names()

    async def test_ensure_folder_on_an_existing_folder_outside_is_refused_not_already_existed(self, w):
        await refused(w.service.ensure_folder(w.handle, "Coimbra 2024"), OUT_WRITE)

    async def test_ensure_folder_inside_creates(self, w):
        out = await w.service.ensure_folder(w.handle, "R&D/New")
        assert out.created == ["R&D/New"]

    async def test_empty_folder_save_lands_in_inbox_inside_the_write_folder(self, w):
        out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", None)
        assert out.item.path == f"R&D/{DEFAULT_INBOX_FOLDER}/a.txt"

    async def test_root_write_folder_inbox_is_a_top_level_inbox(self):
        w = World()
        out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", "")
        assert out.item.path == f"{DEFAULT_INBOX_FOLDER}/a.txt"

    async def test_append_replace_delete_outside_refuse_without_the_path(self, w):
        await refused(w.service.append_text(w.handle, w.notes, "more"), OUT_WRITE)
        await refused(w.service.replace_with(w.handle, w.notes, b"new", "text/plain"), OUT_WRITE)
        await refused(w.service.delete(w.handle, w.lab), OUT_WRITE)
        assert w.made() == []

    async def test_append_to_a_folder_outside_does_not_print_its_path(self, w):
        # N3: the "not a text file" text names the path; the check runs first
        await refused(w.service.append_text(w.handle, w.coimbra, "more"), OUT_WRITE)
        await refused(w.service.replace_with(w.handle, w.coimbra, b"x", "text/plain"), OUT_WRITE)

    async def test_append_inside_is_allowed(self, w):
        log = w.fake.plant(w.handle, w.rd, "log.txt", b"a\n")
        out = await w.service.append_text(w.handle, log, "b")
        assert out.mode == "append"

    async def test_move_inside_destination_outside_is_refused_and_creates_nothing(self, w):
        await refused(w.service.move(w.handle, w.plan, "Coimbra 2024/New", None), OUT_WRITE)
        assert w.made() == []

    async def test_move_source_outside_is_refused(self, w):
        await refused(w.service.move(w.handle, w.lab, "R&D", None), OUT_WRITE)
        assert w.made() == []

    async def test_rename_inside_is_allowed_and_the_write_folder_is_protected(self, w):
        out = await w.service.move(w.handle, w.plan, None, "Tom's plan 2.docx")
        assert out.after.name == "Tom's plan 2.docx"
        await refused(w.service.move(w.handle, w.rd, None, "R&D 2"), PROTECTED_LINE.format(drive="Acme"))
        await refused(w.service.delete(w.handle, w.rd), PROTECTED_LINE.format(drive="Acme"))

    async def test_move_into_itself_text_is_not_reached_for_an_item_outside(self, w):
        await refused(w.service.move(w.handle, w.coimbra, "Coimbra 2024/Sub", None), OUT_WRITE)


class TestScope:
    @pytest.fixture
    def w(self):
        w = World()
        w.settings(scope_folder_ids=(w.rd,), write_folder_id=w.rd)
        return w

    async def test_list_inside_and_outside(self, w):
        assert (await w.service.list_folder(w.handle, "R&D")).folder.item_id == w.rd
        await refused(w.service.list_folder(w.handle, "Coimbra 2024"), OUT_SCOPE)

    async def test_a_missing_folder_outside_the_scope_is_no_oracle(self, w):
        await refused(w.service.list_folder(w.handle, "Coimbra 2024/Nope"), OUT_SCOPE)
        await refused(w.service.list_folder(w.handle, "Coimbra 2024"), OUT_SCOPE)

    async def test_a_missing_folder_inside_the_scope_is_still_missing(self, w):
        from src.domain.user_drive import DrivePathError
        with pytest.raises(DrivePathError):
            await w.service.list_folder(w.handle, "R&D/Nope")

    async def test_list_by_id_checks_before_the_file_text(self, w):
        await refused(w.service.list_folder_by_id(w.handle, w.lab), OUT_SCOPE)
        assert (await w.service.list_folder_by_id(w.handle, w.rd)).folder.item_id == w.rd

    async def test_get_item_reads_inside_only(self, w):
        assert (await w.service.get_item(w.handle, w.plan)).item_id == w.plan
        await refused(w.service.get_item(w.handle, w.lab), OUT_SCOPE)

    async def test_search_keeps_the_scope(self, w):
        found = await w.service.search(w.handle, "notes")
        assert [i.item_id for i in found] == []
        found = await w.service.search(w.handle, "plan")
        assert [i.item_id for i in found] == [w.plan]

    async def test_a_write_folder_moved_out_of_the_scope_accepts_nothing(self, w):
        w.settings(scope_folder_ids=(w.coimbra,), write_folder_id=w.rd)
        await refused(w.service.save(w.handle, b"x", "a.txt", "text/plain", "R&D"), OUT_SCOPE)
        assert w.made() == []


class TestOneConnectionReadPerOperation:
    """E-13: the policy reads the connection once per operation; a save into the Inbox adds none."""

    @pytest.mark.parametrize("write_folder", ["", "rd"])
    async def test_a_save_into_the_inbox_reads_the_connection_once(self, write_folder):
        w = World()
        w.settings(write_folder_id=w.rd if write_folder else write_folder)
        reads = []
        original = w.repo.get

        async def counting(user_id, connection_id):
            reads.append(connection_id)
            return await original(user_id, connection_id)
        w.repo.get = counting
        out = await w.service.save(w.handle, b"x", "a.txt", "text/plain", None)
        assert out.item.path.endswith(f"{DEFAULT_INBOX_FOLDER}/a.txt")
        assert len(reads) == 1
