"""WorkspaceService — copy (AGENT_WORKSPACE_RFC §7.3 copy, D10): chat uploads and delivered documents through
FileConversionService.resolve_bytes, workspace to workspace through the port."""
from unittest.mock import AsyncMock

from src.domain.workspace import MAX_FILES_PER_USER, MAX_FILE_BYTES, WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"


def _conversion(result=b"%PDF-1.4 lease"):
    conversion = AsyncMock()
    if isinstance(result, Exception) or isinstance(result, list):
        conversion.resolve_bytes = AsyncMock(side_effect=result)
    else:
        conversion.resolve_bytes = AsyncMock(return_value=result)
    return conversion


def _svc(conversion=None):
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store, conversion), store


def _call(known=None):
    return WorkspaceCall(user_id=USER, origin="chat", known=dict(known or {}))


class TestCopyFromChat:
    async def test_upload_into_files(self):
        conversion = _conversion()
        svc, store = _svc(conversion)
        r = await svc.run(_call(), "copy", {"source": '"lease (1).pdf"', "path": "files/lease.pdf"})
        [e] = store.versions_of(USER, "files/lease.pdf")
        assert r.text == f"copied lease (1).pdf to workspace:files/lease.pdf · 14 B · generation {e.generation}"
        assert dict(r.generations) == {"files/lease.pdf": e.generation}
        conversion.resolve_bytes.assert_awaited_once_with("lease (1).pdf", USER)
        assert store.calls == [("list", ""), ("put", "files/lease.pdf")]

    async def test_delivered_document_key(self):
        conversion = _conversion(b"<html></html>")
        svc, store = _svc(conversion)
        r = await svc.run(_call(), "copy", {"source": "docs/u1/report.html", "path": "files/report.html"})
        assert not r.failed
        conversion.resolve_bytes.assert_awaited_once_with("docs/u1/report.html", USER)

    async def test_a_text_upload_may_go_into_projects(self):
        """A1: the owner's long instruction lands in projects/ byte for byte, an ordinary editable file (§0.2)."""
        conversion = _conversion("Morning briefing\n1. Read the news.".encode("utf-8"))
        svc, store = _svc(conversion)
        r = await svc.run(_call(), "copy", {"source": "briefing.txt", "path": "projects/morning-briefing.md"})
        assert not r.failed
        assert store.live_text(USER, "projects/morning-briefing.md") == "Morning briefing\n1. Read the news."

    async def test_a_binary_upload_goes_only_into_files(self):
        svc, store = _svc(_conversion(b"%PDF-1.4 \xff\xfe"))
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "projects/lease.pdf"})
        assert r.text == ("SYSTEM: invalid workspace path 'projects/lease.pdf': only text files go outside files/; "
                          "copy binary files under files/.")
        assert store.versions_of(USER, "projects/lease.pdf") == []

    async def test_drive_and_skill_sources_are_refused(self):
        svc, _ = _svc(_conversion())
        for source, tool in (("drive:01ABC", "work_with_drive_files"), ("skill:x/r.md", "open_file")):
            r = await svc.run(_call(), "copy", {"source": source, "path": "files/x.pdf"})
            assert r.failed and tool in r.text

    async def test_missing_and_foreign_sources(self):
        svc, _ = _svc(_conversion([FileNotFoundError("x"), PermissionError("y")]))
        r = await svc.run(_call(), "copy", {"source": "x.pdf", "path": "files/x.pdf"})
        assert r.text == ("SYSTEM: 'x.pdf' not found — pass the file name from its [File: …] label, or the "
                          "delivered-document key.")
        r = await svc.run(_call(), "copy", {"source": "docs/u2/x.pdf", "path": "files/x.pdf"})
        assert r.text == "SYSTEM: 'docs/u2/x.pdf' does not belong to this user."

    async def test_without_file_conversion(self):
        svc, _ = _svc(None)
        r = await svc.run(_call(), "copy", {"source": "x.pdf", "path": "files/x.pdf"})
        assert r.text == "SYSTEM: copying chat files and delivered documents is unavailable here."

    async def test_needs_a_source(self):
        svc, _ = _svc(_conversion())
        r = await svc.run(_call(), "copy", {"source": "", "path": "files/x.pdf"})
        assert r.failed and "copy needs a source" in r.text

    async def test_size_cap(self):
        svc, _ = _svc(_conversion(b"x" * (MAX_FILE_BYTES + 1)))
        r = await svc.run(_call(), "copy", {"source": "big.pdf", "path": "files/big.pdf"})
        assert r.text == "SYSTEM: files/big.pdf: file over 10 MB."


class TestCopyWithinTheWorkspace:
    async def test_text_copy_into_a_text_folder(self):
        svc, store = _svc()
        store.seed(USER, "scratch/sources.md", "a\nb")
        r = await svc.run(_call(), "copy", {"source": "workspace:scratch/sources.md", "path": "references/sources.md"})
        assert not r.failed and store.live_text(USER, "references/sources.md") == "a\nb"

    async def test_bare_workspace_path_source(self):
        svc, store = _svc()
        store.seed(USER, "scratch/sources.md", "a")
        r = await svc.run(_call(), "copy", {"source": "scratch/sources.md", "path": "projects/sources.md"})
        assert not r.failed

    async def test_binary_cannot_go_to_a_text_folder(self):
        svc, store = _svc()
        store.seed(USER, "files/a.png", b"\x89PNG\xff\xfe")  # not UTF-8: binary, whatever the new extension says
        r = await svc.run(_call(), "copy", {"source": "files/a.png", "path": "projects/a.md"})
        assert r.failed and "copy binary files under files/" in r.text

    async def test_missing_workspace_source(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "copy", {"source": "scratch/nope.md", "path": "projects/nope.md"})
        assert r.text == "SYSTEM: workspace:scratch/nope.md does not exist; view it to see its history."


class TestCopyDestination:
    async def test_existing_destination_without_a_view(self):
        svc, store = _svc(_conversion())
        store.seed(USER, "files/lease.pdf", b"old")
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "files/lease.pdf"})
        assert r.text == ("SYSTEM: files/lease.pdf exists. View it first to overwrite it with copy, or pick another "
                          "path.")

    async def test_overwrite_after_a_view(self):
        svc, store = _svc(_conversion(b"new"))
        e = store.seed(USER, "files/lease.pdf", b"old")
        store.calls.clear()
        r = await svc.run(_call(known={"files/lease.pdf": e.generation}), "copy",
                          {"source": "lease.pdf", "path": "files/lease.pdf"})
        assert not r.failed and store.live_text(USER, "files/lease.pdf") == "new"
        assert store.calls == [("list", ""), ("put", "files/lease.pdf")]  # list: the byte cap on an overwrite

    async def test_cap(self):
        svc, store = _svc(_conversion())
        for i in range(MAX_FILES_PER_USER):
            store.seed(USER, f"projects/f{i}.md", "x")
        r = await svc.run(_call(), "copy", {"source": "lease.pdf", "path": "files/lease.pdf"})
        assert r.text == "SYSTEM: workspace full (100 files); delete or merge first."
