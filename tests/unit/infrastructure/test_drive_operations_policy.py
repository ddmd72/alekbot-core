"""DriveOperations over the access policy (MULTI_DRIVE_RFC §11.3, §11.5; slice 2b P-B): a change on a read-only second
drive answers NO_WRITE_FOLDER_LINE (REFUSED) by path and by ref; a write folder `Acme/R&D` admits `Acme/R&D` and refuses
`Acme/Coimbra 2024`; an empty-folder save lands in `<write folder>/Inbox` of the primary; no primary answers
NO_PRIMARY_LINE; a replace sourcing another drive is a copy across drives (CROSS_DRIVE_LINE); `folder_path` keeps the
policy's refusal; the preflight and `/` lines show the write folder and the bounded note; a search outcome with two
tags builds two-tag `drive_context`."""
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_access_policy import NO_WRITE_FOLDER_LINE, OUTSIDE_SCOPE_LINE, OUTSIDE_WRITE_FOLDER_LINE
from src.domain.drive_mounts import CROSS_DRIVE_LINE, NO_PRIMARY_LINE, NOTE_LINE_MAX
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.drive_search import STATUS_OK, DriveSearchCard, DriveSearchResult
from src.domain.drive_user_settings import DriveUserSettings
from src.domain.user_drive import DriveAccessRefused
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import connection_service, seed
from tests.support.fake_drive_adapter import FakeDriveAdapter

T1 = datetime(2026, 1, 2, tzinfo=timezone.utc)
CTX = {"account_id": "acc-1"}
MUTATING = {"create_folder", "upload", "replace_content", "move", "delete"}


class Notes:
    def __init__(self):
        self.texts = {}

    async def drive_note_texts(self, user_id):
        return self.texts


class World:
    pass


@pytest.fixture
async def w():
    w = World()
    w.repo = FakeDriveConnectionRepository()
    w.fake = FakeDriveAdapter("fake_a")
    connections = connection_service(w.repo)
    w.one = await seed(w.repo, connection_id="conn-1", tag="a1")
    w.two = await seed(w.repo, connection_id="conn-2", tag="b2", kind="fake_b", primary=False, created_at=T1)
    await w.repo.update("usr-1", "conn-2", mount_name="Acme")
    def set_two(**changes):
        w.repo.connections[("usr-1", "conn-2")] = replace(w.repo.connections[("usr-1", "conn-2")], **changes)
    w.set_two = set_two
    w.set_two(write_folder_id=None)
    root1 = w.fake.tree(w.one.handle).root.item_id
    root2 = w.fake.tree(w.two.handle).root.item_id
    w.rd = w.fake.plant(w.two.handle, root2, "R&D", folder=True)
    w.plan = w.fake.plant(w.two.handle, w.rd, "Tom's plan.docx", b"plan")
    w.coimbra = w.fake.plant(w.two.handle, root2, "Coimbra 2024", folder=True)
    w.lab = w.fake.plant(w.two.handle, w.coimbra, "lab-report.pdf", b"lab")
    w.mine = w.fake.plant(w.one.handle, root1, "mine.txt", b"mine")
    w.notes = Notes()
    service = UserDriveService(w.fake, connections)
    conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=w.fake,
                                       connections=connections, access=service)
    w.search = AsyncMock()
    w.search.search_states.return_value = {}
    w.ops = DriveOperations(conversion_service=conversion, drive_service=service, drive_notes=w.notes,
                            drive_search=w.search)
    w.service = service
    w.fake.calls.clear()
    return w


def made(w):
    return [m for m, _ in w.fake.calls if m in MUTATING]


def ref2(item_id):
    return f"drive:b2:{item_id}"


class TestReadOnlySecondDrive:
    async def test_a_save_by_path_is_refused(self, w):
        out = await w.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", folder="Acme/R&D", wait_s=5)
        assert not out.success and out.cls is OutcomeClass.REFUSED and out.text == NO_WRITE_FOLDER_LINE
        assert made(w) == []

    async def test_create_folder_by_path_is_refused(self, w):
        out = await w.ops.create_folder(CTX, "usr-1", "Acme/New", wait_s=5)
        assert out.cls is OutcomeClass.REFUSED and out.text == NO_WRITE_FOLDER_LINE and made(w) == []

    async def test_move_append_replace_delete_by_ref_are_refused(self, w):
        outs = [
            await w.ops.move(CTX, "usr-1", ref2(w.plan), new_name="x.docx", wait_s=5),
            await w.ops.append(CTX, "usr-1", ref2(w.plan), "more", wait_s=5),
            await w.ops.replace(CTX, "usr-1", ref2(w.plan), "drive:b2:" + w.lab, wait_s=5),
            await w.ops.delete(CTX, "usr-1", ref2(w.plan), wait_s=5),
        ]
        assert [(o.cls, o.text) for o in outs] == [(OutcomeClass.REFUSED, NO_WRITE_FOLDER_LINE)] * 4
        assert made(w) == []


class TestWriteFolder:
    async def test_a_save_inside_lands_and_one_outside_is_refused(self, w):
        w.set_two(write_folder_id=w.rd)
        ok = await w.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", folder="Acme/R&D", wait_s=5)
        assert ok.success and "Acme/R&D/note.md" in ok.text
        out = await w.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", folder="Acme/Coimbra 2024", wait_s=5)
        assert out.cls is OutcomeClass.REFUSED and out.text == OUTSIDE_WRITE_FOLDER_LINE.format(drive="Acme")
        assert made(w) == ["upload"]

    async def test_a_refusal_never_prints_the_path(self, w):
        w.set_two(write_folder_id=w.rd)
        for out in (await w.ops.delete(CTX, "usr-1", ref2(w.lab), wait_s=5),
                    await w.ops.append(CTX, "usr-1", ref2(w.coimbra), "x", wait_s=5),
                    await w.ops.move(CTX, "usr-1", ref2(w.coimbra), new_name="x", wait_s=5)):
            assert out.cls is OutcomeClass.REFUSED and "Coimbra" not in out.text and "lab-report" not in out.text

    async def test_a_scope_hides_what_is_outside(self, w):
        w.set_two(scope_folder_ids=(w.rd,), write_folder_id=w.rd)
        out = await w.ops.open(CTX, "usr-1", ref2(w.lab))
        assert out.cls is OutcomeClass.REFUSED and out.text == OUTSIDE_SCOPE_LINE.format(drive="Acme")

    async def test_a_cross_drive_move_still_says_so(self, w):
        w.set_two(write_folder_id="")
        out = await w.ops.move(CTX, "usr-1", ref2(w.plan), folder="Drive/Elsewhere", wait_s=5)
        assert out.cls is OutcomeClass.REFUSED and out.text == CROSS_DRIVE_LINE and made(w) == []


class TestReplaceAcrossDrives:
    async def test_a_source_on_another_drive_is_refused_before_its_bytes_are_read(self, w):
        w.set_two(write_folder_id="")
        out = await w.ops.replace(CTX, "usr-1", ref2(w.plan), f"drive:a1:{w.mine}", wait_s=5)
        assert out.cls is OutcomeClass.REFUSED and out.text == CROSS_DRIVE_LINE
        assert made(w) == [] and "download" not in [m for m, _ in w.fake.calls]

    async def test_a_source_on_the_same_drive_replaces(self, w):
        w.set_two(write_folder_id="")
        out = await w.ops.replace(CTX, "usr-1", ref2(w.plan), ref2(w.lab), wait_s=5)
        assert out.success and w.fake.tree(w.two.handle).nodes[w.plan].data == b"lab"


class TestInbox:
    async def test_an_empty_folder_lands_in_the_primarys_inbox_inside_its_write_folder(self, w):
        root1 = w.fake.tree(w.one.handle).root.item_id
        base = w.fake.plant(w.one.handle, root1, "Springfield", folder=True)
        w.repo.connections[("usr-1", "conn-1")] = replace(w.repo.connections[("usr-1", "conn-1")],
                                                          write_folder_id=base)
        out = await w.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", wait_s=5)
        assert out.success and "Drive/Springfield/Inbox/note.md" in out.text

    async def test_no_primary_is_refused(self, w):
        await w.repo.save_settings(DriveUserSettings(user_id="usr-1"))
        out = await w.ops.save_text(CTX, "usr-1", "hello", file_name="note.md", wait_s=5)
        assert out.cls is OutcomeClass.REFUSED and out.text == NO_PRIMARY_LINE and made(w) == []


class TestFolderPath:
    async def test_the_policys_refusal_is_not_turned_into_a_path(self, w):
        w.set_two(scope_folder_ids=(w.rd,))
        with pytest.raises(DriveAccessRefused) as caught:
            await w.ops.folder_path("usr-1", ref2(w.coimbra))
        assert caught.value.line == OUTSIDE_SCOPE_LINE.format(drive="Acme")

    async def test_a_readable_folder_ref_becomes_its_canonical_path(self, w):
        assert await w.ops.folder_path("usr-1", ref2(w.rd)) == "Acme/R&D"


class TestMountLines:
    async def test_the_preflight_shows_the_write_folder_and_the_bounded_note(self, w):
        w.set_two(write_folder_id=w.rd)
        w.notes.texts = {"conn-2": "Acme work files: R&D, Tom's plan " + "x" * (NOTE_LINE_MAX + 1)}
        preflight = await w.ops.preflight("usr-1")
        line = next(m for m in preflight.mounts if m.name == "Acme")
        assert line.writable and line.write_folder == "Acme/R&D"
        assert line.note.startswith("Acme work files: R&D, Tom's plan") and len(line.note) == NOTE_LINE_MAX + 1
        assert next(m for m in preflight.mounts if m.name != "Acme").write_folder == ""

    async def test_the_slash_listing_shows_them_too(self, w):
        w.set_two(write_folder_id=w.rd)
        w.notes.texts = {"conn-2": "Acme work files: R&D, Tom's plan"}
        out = await w.ops.list_folder(CTX, "usr-1", "/")
        assert "- Acme: " in out.text and "writable only in Acme/R&D" in out.text
        assert 'owner\'s note: "Acme work files: R&D, Tom\'s plan"' in out.text

    async def test_a_failed_write_folder_read_leaves_the_path_out(self, w):
        w.set_two(write_folder_id="item-gone")
        line = next(m for m in (await w.ops.preflight("usr-1")).mounts if m.name == "Acme")
        assert line.writable and line.write_folder == ""

    async def test_a_failed_notes_read_leaves_the_notes_out(self, w):
        async def broken(user_id):
            raise RuntimeError("down")
        w.notes.drive_note_texts = broken
        assert all(m.note == "" for m in (await w.ops.preflight("usr-1")).mounts)


class TestSearchTags:
    async def test_two_connections_build_two_tag_drive_context(self, w):
        def card(item_id, tag):
            return DriveSearchCard(ref=f"drive:{tag}:{item_id}", item_id=item_id, name=f"{item_id}.pdf",
                                   path=f"{item_id}.pdf", kind="pdf", is_folder=False, summary="s", nonce="abcdef12",
                                   ref_tag=tag)
        w.search.search.return_value = DriveSearchResult(status=STATUS_OK, cards=(card("f1", "a1"), card("f2", "b2")),
                                                         ref_tag="")
        out = await w.ops.search(CTX, "usr-1", query="lab")
        assert out.item_tags == ("a1", "b2")
        assert [e["ref"] for e in out.drive_context()] == ["drive:a1:f1", "drive:b2:f2"]
