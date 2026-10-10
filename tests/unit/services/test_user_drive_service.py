"""UserDriveService rules (docs/10_rfcs/USER_DRIVE_RFC.md §4.7–§4.11)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import DriveItem, DriveNameConflictError, DrivePathError, DriveRootProtectedError
from src.ports.user_drive_port import UserDrivePort
from src.services.user_drive_service import LIST_LIMIT, UserDriveService

ROOT = DriveItem(item_id="root", name="Alek-bot", path="", is_folder=True)
INBOX = DriveItem(item_id="inbox", name="Inbox", path="Inbox", is_folder=True)


def _folder(item_id, name, parent=""):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name, is_folder=True)


def _file(item_id, name, parent="", size=10, mime="text/plain"):
    return DriveItem(item_id=item_id, name=name, path=f"{parent}/{name}" if parent else name,
                     is_folder=False, size_bytes=size, mime_type=mime)


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDrivePort)
    d.get_root.return_value = ROOT
    return d


@pytest.fixture
def service(drive):
    return UserDriveService(drive)


class TestResolveFolder:
    async def test_existing_case_insensitive_no_create(self, service, drive):
        drive.list_children.side_effect = [[_folder("m", "Встречи")], [_folder("y", "2026", "Встречи")]]
        res = await service.resolve_folder("u1", "встречи/2026", create=True)
        assert res.folder.item_id == "y" and res.created == []
        drive.create_folder.assert_not_called()

    async def test_missing_segments_created(self, service, drive):
        drive.list_children.side_effect = [[_folder("m", "Встречи")], []]
        drive.create_folder.return_value = _folder("n", "2026", "Встречи")
        res = await service.resolve_folder("u1", "Встречи/2026", create=True)
        drive.create_folder.assert_awaited_once_with("u1", "m", "2026")
        assert res.created == ["Встречи/2026"]

    async def test_create_race_uses_winner(self, service, drive):
        drive.list_children.side_effect = [[], [INBOX]]
        drive.create_folder.side_effect = DriveNameConflictError("taken")
        res = await service.resolve_folder("u1", "Inbox", create=True)
        assert res.folder is INBOX and res.created == []

    async def test_blank_is_root_and_escape_rejected(self, service):
        assert (await service.resolve_folder("u1", "  ", create=False)).folder is ROOT
        with pytest.raises(DrivePathError):
            await service.resolve_folder("u1", "../x", create=True)


class TestSaveDuplicates:
    async def test_default_inbox_new_file(self, service, drive):
        drive.list_children.side_effect = [[], []]
        drive.create_folder.return_value = INBOX
        drive.upload.return_value = _file("f", "a.pdf", "Inbox")
        out = await service.save("u1", b"x", "a.pdf", "application/pdf", None)
        drive.upload.assert_awaited_once_with("u1", "inbox", "a.pdf", b"x", "application/pdf")
        assert out.created == ["Inbox"] and not out.already_existed and not out.renamed

    async def test_same_name_same_bytes_not_uploaded(self, service, drive):
        existing = _file("e", "a.pdf", "Inbox", size=3)
        drive.list_children.side_effect = [[INBOX], [existing]]
        drive.download.return_value = b"abc"
        out = await service.save("u1", b"abc", "a.pdf", "application/pdf", None)
        assert out.already_existed and out.item is existing
        drive.upload.assert_not_called()

    async def test_nfd_existing_name_is_the_same_file(self, service, drive):
        import unicodedata
        existing = _file("e", unicodedata.normalize("NFD", "Отчёт.pdf"), "Inbox", size=3)
        drive.list_children.side_effect = [[INBOX], [existing]]
        drive.download.return_value = b"abc"
        out = await service.save("u1", b"abc", "Отчёт.pdf", "application/pdf", None)
        assert out.already_existed
        drive.upload.assert_not_called()

    async def test_same_name_different_size_uploaded_renamed(self, service, drive):
        drive.list_children.side_effect = [[INBOX], [_file("e", "a.pdf", "Inbox", size=99)]]
        drive.upload.return_value = _file("n", "a 1.pdf", "Inbox")  # provider suffix format, spike A8: "<stem> 1<ext>"
        out = await service.save("u1", b"abc", "a.pdf", "application/pdf", None)
        drive.download.assert_not_called()
        assert out.renamed and out.item.name == "a 1.pdf"


class TestUpdate:
    async def test_append_adds_line(self, service, drive):
        note = _file("n", "todo.md", "Notes", size=5, mime="text/markdown")
        drive.get_item.return_value = note
        drive.download.return_value = b"- one"
        drive.replace_content.return_value = note
        out = await service.append_text("u1", "n", "- two")
        drive.replace_content.assert_awaited_once_with("u1", "n", b"- one\n- two\n", "text/markdown")
        assert out.mode == "append" and out.before_size == 5

    async def test_append_refuses_binary(self, service, drive):
        drive.get_item.return_value = _file("p", "a.pdf", size=5, mime="application/pdf")
        with pytest.raises(DrivePathError):
            await service.append_text("u1", "p", "x")

    async def test_replace_refuses_folder(self, service, drive):
        drive.get_item.return_value = _folder("m", "Встречи")
        with pytest.raises(DrivePathError):
            await service.replace_with("u1", "m", b"x", "text/plain")


class TestDeleteMove:
    async def test_delete_root_refused(self, service, drive):
        with pytest.raises(DriveRootProtectedError):
            await service.delete("u1", "root")
        drive.delete.assert_not_called()

    async def test_folder_count_walks_subtree(self, service, drive):
        top = _folder("t", "Встречи")
        drive.get_item.return_value = top
        drive.list_children.side_effect = [[_folder("s", "2025", "Встречи"), _file("a", "a")],
                                           [_file("b", "b"), _file("c", "c")]]
        out = await service.delete("u1", "t")
        assert out.file_count == 3 and out.item is top
        drive.delete.assert_awaited_once_with("u1", "t")

    async def test_move_root_refused(self, service):
        with pytest.raises(DriveRootProtectedError):
            await service.move("u1", "root", "X", None)

    async def test_rename_only(self, service, drive):
        before = _file("a", "a.txt", "Inbox")
        drive.get_item.return_value = before
        drive.move.return_value = _file("a", "b.txt", "Inbox")
        out = await service.move("u1", "a", None, "b.txt")
        drive.move.assert_awaited_once_with("u1", "a", new_parent_id=None, new_name="b.txt")
        assert out.before is before and out.after.path == "Inbox/b.txt"

    async def test_move_needs_folder_or_name(self, service):
        with pytest.raises(DrivePathError):
            await service.move("u1", "a", None, None)


async def test_list_sorted_and_truncated(service, drive):
    drive.list_children.return_value = [_file(str(i), f"f{i:03}") for i in range(LIST_LIMIT + 5)] + [_folder("z", "Zeta")]
    out = await service.list_folder("u1", None)
    assert out.items[0].name == "Zeta" and len(out.items) == LIST_LIMIT and out.truncated


class TestProviderInvalidNames:
    """Segments and new names the provider would reject surface as DrivePathError, before any call."""

    @pytest.mark.parametrize("bad", ["a:b", "Что?", 'q"x', "a*b", "x|y", "<t>", "dots.."])
    async def test_invalid_folder_segment_rejected_before_provider(self, service, drive, bad):
        with pytest.raises(DrivePathError):
            await service.resolve_folder("u1", f"Ok/{bad}", create=True)
        drive.create_folder.assert_not_called()

    async def test_invalid_segment_rejected_even_without_create(self, service, drive):
        with pytest.raises(DrivePathError):
            await service.resolve_folder("u1", "a:b", create=False)
        drive.list_children.assert_not_called()

    async def test_invalid_new_name_rejected(self, service, drive):
        drive.get_item.return_value = _file("a", "a.txt", "Inbox")
        with pytest.raises(DrivePathError):
            await service.move("u1", "a", None, "a:b.txt")
        drive.move.assert_not_called()

    async def test_blank_name_and_blank_folder_is_no_patch(self, service, drive):
        with pytest.raises(DrivePathError):
            await service.move("u1", "a", "  ", "  ")
        drive.move.assert_not_called()


class TestReviewGaps:
    """Rules the plan's tests left unpinned; added in the Task 5 review."""

    async def test_ensure_folder_blank_rejected(self, service, drive):
        with pytest.raises(DrivePathError):
            await service.ensure_folder("u1", " / ")
        drive.get_root.assert_not_called()

    async def test_ensure_folder_existing_reports_nothing_created(self, service, drive):
        drive.list_children.return_value = [INBOX]
        res = await service.ensure_folder("u1", "inbox")
        assert res.folder is INBOX and res.created == []

    async def test_name_taken_by_a_file_is_a_path_error(self, service, drive):
        """A 409 whose re-list shows no folder (a file holds the name) is a DrivePathError, not a raw conflict."""
        drive.list_children.side_effect = [[_file("f", "Inbox")], [_file("f", "Inbox")]]
        drive.create_folder.side_effect = DriveNameConflictError("taken")
        with pytest.raises(DrivePathError, match="already exists as a file"):
            await service.resolve_folder("u1", "Inbox", create=True)

    async def test_list_missing_folder_is_path_error_without_create(self, service, drive):
        drive.list_children.return_value = []
        with pytest.raises(DrivePathError):
            await service.list_folder("u1", "Nope")
        drive.create_folder.assert_not_called()

    async def test_append_size_cap_checked_before_download(self, service, drive):
        from src.domain.user_drive import MAX_DRIVE_APPEND_FILE_BYTES
        drive.get_item.return_value = _file("n", "big.md", size=MAX_DRIVE_APPEND_FILE_BYTES + 1, mime="text/markdown")
        with pytest.raises(DrivePathError):
            await service.append_text("u1", "n", "x")
        drive.download.assert_not_called()

    async def test_append_refuses_non_utf8(self, service, drive):
        drive.get_item.return_value = _file("n", "notes.txt", size=4)
        drive.download.return_value = b"\xff\xfe\x00\x01"
        with pytest.raises(DrivePathError):
            await service.append_text("u1", "n", "x")
        drive.replace_content.assert_not_called()

    async def test_append_after_trailing_newline_adds_no_blank_line(self, service, drive):
        note = _file("n", "todo.md", size=6, mime="")
        drive.get_item.return_value = note
        drive.download.return_value = b"- one\n"
        drive.replace_content.return_value = note
        await service.append_text("u1", "n", "- two\n")
        drive.replace_content.assert_awaited_once_with("u1", "n", b"- one\n- two\n", "text/plain")

    async def test_folder_count_capped(self, service, drive):
        from src.services.user_drive_service import COUNT_CAP
        drive.get_item.return_value = _folder("t", "Big")
        drive.list_children.return_value = [_file(str(i), f"f{i}") for i in range(COUNT_CAP + 1)]
        out = await service.delete("u1", "t")
        assert out.file_count == COUNT_CAP and out.count_capped

    async def test_delete_file_counts_one_without_listing(self, service, drive):
        drive.get_item.return_value = _file("a", "a.txt", "Inbox")
        out = await service.delete("u1", "a")
        assert out.file_count == 1 and not out.count_capped
        drive.list_children.assert_not_called()

    async def test_move_into_folder_creates_missing_and_reports(self, service, drive):
        drive.get_item.return_value = _file("a", "a.txt", "Inbox")
        drive.list_children.return_value = []
        drive.create_folder.return_value = _folder("m", "Встречи")
        drive.move.return_value = _file("a", "a.txt", "Встречи")
        out = await service.move("u1", "a", "встречи", None)
        drive.move.assert_awaited_once_with("u1", "a", new_parent_id="m", new_name=None)
        assert out.created == ["Встречи"] and out.after.path == "Встречи/a.txt"

    async def test_move_missing_item_creates_no_folder(self, service, drive):
        from src.domain.user_drive import DriveItemNotFoundError
        drive.get_item.side_effect = DriveItemNotFoundError("gone")
        with pytest.raises(DriveItemNotFoundError):
            await service.move("u1", "a", "New", None)
        drive.create_folder.assert_not_called()

    @pytest.mark.parametrize("dest", ["Встречи", "встречи/2026", "Встречи/2026/new"])
    async def test_move_folder_into_own_subtree_refused_before_any_create(self, service, drive, dest):
        drive.get_item.return_value = _folder("m", "Встречи")
        with pytest.raises(DrivePathError):
            await service.move("u1", "m", dest, None)
        drive.list_children.assert_not_called()
        drive.create_folder.assert_not_called()
        drive.move.assert_not_called()

    async def test_move_folder_to_its_parent_or_sibling_allowed(self, service, drive):
        sub = _folder("s", "2025", "Встречи")
        drive.get_item.return_value = sub
        drive.list_children.return_value = [_folder("m", "Встречи")]
        drive.move.return_value = _folder("s", "2025", "Встречи")
        out = await service.move("u1", "s", "Встречи", None)
        assert out.after is drive.move.return_value
