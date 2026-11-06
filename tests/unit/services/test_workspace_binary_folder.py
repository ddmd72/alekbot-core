"""binary_files/ holds binary files only, and copy accepts a drive source (AGENT_WORKSPACE_RFC D10, §6, §7.3 copy, A6)."""
from unittest.mock import AsyncMock

import pytest

from src.domain.user_drive import (
    DriveFileTooLargeError,
    DriveItem,
    DriveItemNotFoundError,
    DriveNotConnectedError,
    DrivePathError,
    DriveRefRefused,
)
from src.domain.workspace import MAX_FILE_BYTES, MAX_TEXT_FILE_BYTES, WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
PDF = b"%PDF-1.4 \xff\xfe"


def _conversion(result):
    conversion = AsyncMock()
    if isinstance(result, (Exception, list)):
        conversion.resolve_bytes = AsyncMock(side_effect=result)
    else:
        conversion.resolve_bytes = AsyncMock(return_value=result)
    return conversion


def _svc(conversion=None):
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store, conversion), store


def _call(known=None):
    return WorkspaceCall(user_id=USER, origin="chat", known=dict(known or {}))


class TestCopyIntoBinaryFiles:
    async def test_a_binary_upload_lands_byte_for_byte(self):
        svc, store = _svc(_conversion(PDF))
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "binary_files/lease.pdf"})
        assert not r.failed
        [e] = store.versions_of(USER, "binary_files/lease.pdf")
        assert r.text == f"copied lease.pdf to workspace:binary_files/lease.pdf · 11 B · generation {e.generation}"
        assert store.objects[(USER, "binary_files/lease.pdf")][-1][1] == PDF

    async def test_a_binary_file_is_refused_outside_binary_files(self):
        svc, store = _svc(_conversion(PDF))
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "projects/lease.pdf"})
        assert r.text == "Tool note: invalid workspace path 'projects/lease.pdf': binary files go only under binary_files/."
        assert store.versions_of(USER, "projects/lease.pdf") == []

    async def test_a_text_file_is_refused_in_binary_files_with_the_fix(self):
        svc, store = _svc(_conversion(b"plain notes"))
        r = await svc.run(_call(), "copy", {"source": "a.md", "path": "binary_files/a.md"})
        assert r.text == ("Tool note: invalid workspace path 'binary_files/a.md': binary_files/ holds binary files only; "
                          "copy a text file into projects/, references/ or scratch/.")
        assert store.versions_of(USER, "binary_files/a.md") == []

    @pytest.mark.parametrize("folder", ["projects", "references", "scratch"])
    async def test_a_text_file_goes_into_every_text_folder(self, folder):
        svc, store = _svc(_conversion(b"plain notes"))
        r = await svc.run(_call(), "copy", {"source": "a.md", "path": f"{folder}/a.md"})
        assert not r.failed and store.live_text(USER, f"{folder}/a.md") == "plain notes"

    async def test_a_non_utf8_text_extension_file_counts_as_binary(self):
        svc, store = _svc(_conversion(b"\xff\xfe\x00bad"))
        r = await svc.run(_call(), "copy", {"source": "a.md", "path": "projects/a.md"})
        assert r.failed and "binary files go only under binary_files/" in r.text
        r = await svc.run(_call(), "copy", {"source": "a.md", "path": "binary_files/a.md"})
        assert not r.failed and store.versions_of(USER, "binary_files/a.md")

    async def test_a_workspace_text_file_cannot_be_copied_into_binary_files(self):
        svc, store = _svc()
        store.seed(USER, "scratch/a.md", "text")
        r = await svc.run(_call(), "copy", {"source": "workspace:scratch/a.md", "path": "binary_files/a.md"})
        assert r.failed and "binary_files/ holds binary files only" in r.text

    async def test_the_old_folder_name_is_not_a_folder(self):
        svc, _ = _svc(_conversion(PDF))
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "files/lease.pdf"})
        assert r.failed and "start with projects/, references/, scratch/ or binary_files/" in r.text


class TestRulesUnderBinaryFiles:
    async def test_create_and_str_replace_are_refused(self):
        svc, store = _svc()
        store.seed(USER, "binary_files/a.pdf", PDF)
        r = await svc.run(_call(), "create", {"path": "binary_files/a.md", "content": "x"})
        assert r.text == ("Tool note: invalid workspace path 'binary_files/a.md': "
                          "binary_files/ holds binary files only; use copy.")
        r = await svc.run(_call(), "str_replace", {"path": "binary_files/a.md", "old_str": "x", "new_str": "y"})
        assert r.failed and "binary_files/ holds binary files only" in r.text

    async def test_a_binary_file_renames_within_binary_files(self):
        svc, store = _svc()
        store.seed(USER, "binary_files/a.pdf", PDF)
        r = await svc.run(_call(), "rename", {"path": "binary_files/a.pdf", "new_path": "binary_files/b.pdf"})
        assert not r.failed
        assert store.objects[(USER, "binary_files/b.pdf")][-1][1] == PDF

    async def test_a_binary_file_cannot_leave_binary_files(self):
        svc, store = _svc()
        store.seed(USER, "binary_files/a.pdf", PDF)
        r = await svc.run(_call(), "rename", {"path": "binary_files/a.pdf", "new_path": "projects/a.pdf"})
        assert r.text == ("Tool note: invalid workspace path 'projects/a.pdf': "
                          "a file in binary_files/ stays in binary_files/.")

    async def test_nothing_moves_into_binary_files(self):
        svc, store = _svc()
        store.seed(USER, "scratch/a.md", "text")
        r = await svc.run(_call(), "rename", {"path": "scratch/a.md", "new_path": "binary_files/a.md"})
        assert r.text == ("Tool note: invalid workspace path 'binary_files/a.md': "
                          "a file may not move into binary_files/ (it holds binary files only).")

    async def test_the_text_cap_still_applies_to_text_copies(self):
        svc, _ = _svc(_conversion(b"x" * (MAX_TEXT_FILE_BYTES + 1)))
        r = await svc.run(_call(), "copy", {"source": "big.txt", "path": "projects/big.txt"})
        assert r.failed


class TestCopyFromDrive:
    REF = "drive:01ABC"

    async def test_a_drive_pdf_lands_in_binary_files(self):
        conversion = _conversion(PDF)
        svc, store = _svc(conversion)
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert not r.failed
        conversion.resolve_bytes.assert_awaited_once_with(self.REF, USER)
        assert store.objects[(USER, "binary_files/scan.pdf")][-1][1] == PDF

    async def test_a_tagged_drive_text_file_lands_in_projects(self):
        svc, store = _svc(_conversion("garden shed plan".encode()))
        r = await svc.run(_call(), "copy", {"source": "drive:g1:01ABC", "path": "projects/garden-shed.md"})
        assert not r.failed and store.live_text(USER, "projects/garden-shed.md") == "garden shed plan"

    async def test_a_drive_text_file_is_refused_in_binary_files(self):
        svc, _ = _svc(_conversion(b"notes"))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/a.md"})
        assert r.failed and "binary_files/ holds binary files only" in r.text

    async def test_a_drive_binary_is_refused_in_a_text_folder(self):
        svc, _ = _svc(_conversion(PDF))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "references/scan.pdf"})
        assert r.failed and "binary files go only under binary_files/" in r.text

    async def test_a_drive_file_over_the_workspace_cap_is_refused(self):
        svc, store = _svc(_conversion(b"\xff" * (MAX_FILE_BYTES + 1)))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/big.pdf"})
        assert r.text == "Tool note: binary_files/big.pdf: file over 10 MB."
        assert store.versions_of(USER, "binary_files/big.pdf") == []

    async def test_a_refused_ref_shows_the_drive_line(self):
        svc, _ = _svc(_conversion(DriveRefRefused("That drive link is stale; search again")))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.text == "Tool note: That drive link is stale; search again."

    async def test_a_drive_that_is_not_connected(self):
        svc, _ = _svc(_conversion(DriveNotConnectedError()))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.failed and "drive is not connected" in r.text

    async def test_a_file_too_large_for_the_download_cap(self):
        item = DriveItem(item_id="01ABC", name="scan.pdf", path="Garden/scan.pdf", is_folder=False, size_bytes=99)
        svc, _ = _svc(_conversion(DriveFileTooLargeError(item, 10)))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.failed and "too large to copy into the workspace (file cap 10 MB)" in r.text
        assert "Garden/scan.pdf" not in r.text

    async def test_a_folder_is_not_a_file(self):
        svc, _ = _svc(_conversion(DrivePathError("'Garden' is a folder, not a file")))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.failed and "is a folder, not a file" in r.text and "exactly as the drive tool returned it" in r.text

    @pytest.mark.parametrize("error", [FileNotFoundError("x"), DriveItemNotFoundError("x")])
    async def test_a_missing_drive_file(self, error):
        svc, _ = _svc(_conversion(error))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.text == ("Tool note: 'drive:01ABC' not found on the user's drive — pass the file ref exactly as the "
                          "drive tool returned it.")

    async def test_a_foreign_drive_file(self):
        svc, _ = _svc(_conversion(PermissionError("x")))
        r = await svc.run(_call(), "copy", {"source": self.REF, "path": "binary_files/scan.pdf"})
        assert r.text == "Tool note: 'drive:01ABC' does not belong to this user."

    async def test_skill_sources_stay_refused(self):
        conversion = _conversion(b"x")
        svc, _ = _svc(conversion)
        r = await svc.run(_call(), "copy", {"source": "skill:x/r.md", "path": "projects/r.md"})
        assert r.failed and "open_file" in r.text
        conversion.resolve_bytes.assert_not_awaited()

    async def test_a_drive_ref_as_path_stays_refused(self):
        svc, _ = _svc(_conversion(PDF))
        r = await svc.run(_call(), "view", {"path": self.REF})
        assert r.failed
        r = await svc.run(_call(), "copy", {"source": "a.pdf", "path": self.REF})
        assert r.failed
