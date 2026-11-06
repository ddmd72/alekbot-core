"""WorkspaceService grep and the folder tree view (AGENT_WORKSPACE_RFC §7.3–§7.4)."""
import logging

import pytest

from src.domain.workspace import GREP_MAX_PATTERN_CHARS, GREP_MAX_SCAN_BYTES, WorkspaceCall, WorkspaceDeleteMeta
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call():
    return WorkspaceCall(user_id=USER, origin="chat")


def _seeded():
    svc, store = _svc()
    store.seed(USER, "projects/garden-shed.md", "intro\nthe Deadline is friday\noutro")
    store.seed(USER, "projects/trips/packing.md", "deadline")
    store.seed(USER, "references/a.md", "no hit here")
    store.seed(USER, "scratch/big.txt", "deadline in files")
    store.seed(USER, "binary_files/photo.png", b"deadline\x00\xff")
    return svc, store


async def _grep(svc, **args):
    return await svc.run(_call(), "grep", args)


class TestScope:
    async def test_folder_scope(self):
        svc, store = _seeded()
        r = await _grep(svc, pattern="deadline", path="projects/")
        assert not r.failed
        lines = r.text.splitlines()
        assert lines[0] == 'grep "deadline" in projects/ — 1 match in 1 file'
        assert lines[1] == "searched 2 text files: projects/ (2)"
        assert "Deadline" not in r.text  # case-sensitive by default
        assert "projects/trips/packing.md" in r.text
        reads = [c for c in store.calls if c[0] == "read"]
        assert sorted(p for _, p in reads) == ["projects/garden-shed.md", "projects/trips/packing.md"]

    async def test_whole_workspace_skips_binary_and_lists_folders(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="deadline", path="")
        assert r.text.splitlines()[1] == "searched 4 text files: projects/ (2), references/ (1), scratch/ (1)"
        assert "photo.png" not in r.text
        assert "scratch/big.txt" in r.text

    async def test_single_file(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="friday", path="projects/garden-shed.md")
        assert r.text.splitlines() == [
            'grep "friday" in projects/garden-shed.md — 1 match in 1 file',
            "searched 1 text file: projects/garden-shed.md",
            "projects/garden-shed.md",
            "1-intro",
            "2:the Deadline is friday",
            "3-outro",
        ]

    async def test_deleted_files_are_not_searched(self):
        svc, store = _seeded()
        live = store.versions_of(USER, "projects/trips/packing.md")[-1]
        await store.delete(USER, live.path, live.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        r = await _grep(svc, pattern="deadline", path="projects/")
        assert r.text.splitlines()[1] == "searched 1 text file: projects/ (1)"
        assert "packing" not in r.text

    async def test_deleted_single_file_is_not_found(self):
        svc, store = _seeded()
        live = store.versions_of(USER, "references/a.md")[-1]
        await store.delete(USER, live.path, live.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        r = await _grep(svc, pattern="x", path="references/a.md")
        assert r.failed and r.text == "Tool note: references/a.md does not exist; view it to see its history."

    async def test_binary_single_file_is_refused(self):
        svc, store = _seeded()
        r = await _grep(svc, pattern="x", path="binary_files/photo.png")
        assert r.failed and "not a text file" in r.text
        assert not [c for c in store.calls if c[0] == "read"]

    async def test_non_utf8_text_file_is_skipped(self):
        svc, store = _svc()
        store.seed(USER, "scratch/a.txt", b"\xff\xfe deadline")
        store.seed(USER, "scratch/b.txt", "deadline")
        r = await _grep(svc, pattern="deadline", path="scratch/")
        assert r.text.splitlines()[1] == "searched 1 text file: scratch/ (1)"

    async def test_empty_scope(self):
        svc, _ = _svc()  # scratch/ is empty here (the shared seed now keeps a text file in it)
        r = await _grep(svc, pattern="x", path="scratch/")
        assert r.text == 'grep "x" in scratch/ — no matches in 0 files searched\nsearched 0 text files'

    async def test_wrong_store(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="x", path="drive:01ABC")
        assert r.failed and "work_with_drive_files" in r.text


class TestModes:
    async def test_literal_by_default_regex_on_request(self):
        svc, store = _svc()
        store.seed(USER, "projects/a.md", "price (a+b) 5\naab")
        literal = await _grep(svc, pattern="(a+b)", path="projects/")
        assert literal.text.splitlines()[0].endswith("1 match in 1 file")
        regex = await _grep(svc, pattern="a+b", path="projects/", regex=True)
        assert regex.text.splitlines()[0].endswith("1 match in 1 file")
        assert "2:aab" in regex.text

    async def test_ignore_case(self):
        svc, _ = _seeded()
        assert "no matches" in (await _grep(svc, pattern="DEADLINE", path="projects/")).text
        r = await _grep(svc, pattern="DEADLINE", path="projects/", ignore_case="true")
        assert "2 matches in 2 files" in r.text

    async def test_fillers_for_unused_parameters(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="friday", path="projects/", context="", ignore_case="", files_only=None,
                        regex="false", offset="", limit="", depth="")
        assert "2:the Deadline is friday" in r.text and "1-intro" in r.text

    async def test_context_and_files_only(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="friday", path="projects/", context=0)
        assert r.text.splitlines()[2:] == ["projects/garden-shed.md", "2:the Deadline is friday"]
        r = await _grep(svc, pattern="deadline", path="", files_only="true")
        assert r.text.splitlines()[2:] == ["projects/trips/packing.md · 1 match", "scratch/big.txt · 1 match"]
        r = await _grep(svc, pattern="friday", path="projects/", context=-3)
        assert "1-intro" in r.text


class TestFailures:
    @pytest.mark.parametrize("pattern", ["", None])
    async def test_empty_pattern(self, pattern):
        svc, store = _seeded()
        r = await _grep(svc, pattern=pattern, path="projects/")
        assert r.failed and r.text == "Tool note: grep needs a pattern: the text to search for."
        assert not store.calls

    async def test_oversized_pattern(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="x" * (GREP_MAX_PATTERN_CHARS + 1), path="")
        assert r.failed and f"at most {GREP_MAX_PATTERN_CHARS}" in r.text

    async def test_invalid_regex_names_the_engine_message(self):
        svc, store = _seeded()
        r = await _grep(svc, pattern="(unclosed", path="", regex=True)
        assert r.failed and r.text.startswith("Tool note: invalid regex (") and "RE2 syntax" in r.text
        assert not store.calls

    async def test_re2_unsupported_pattern(self):
        svc, _ = _seeded()
        r = await _grep(svc, pattern="(a)\\1", path="", regex=True)
        assert r.failed and "RE2 syntax: no backreferences or lookaround" in r.text

    async def test_scope_over_the_scan_limit_is_refused_before_any_read(self):
        svc, store = _svc()
        for i in range(3):
            store.seed(USER, f"projects/f{i}.md", "deadline", size=GREP_MAX_SCAN_BYTES // 2)
        r = await _grep(svc, pattern="deadline", path="")
        assert r.failed
        assert r.text == "Tool note: 30.0 MB of text in scope, over the 20 MB search limit; narrow path."
        assert not [c for c in store.calls if c[0] == "read"]
        assert [c for c in store.calls] == [("list", "")]

    async def test_binary_bytes_do_not_count_toward_the_limit(self):
        svc, store = _svc()
        store.seed(USER, "binary_files/huge.bin", b"x", size=GREP_MAX_SCAN_BYTES * 2)
        store.seed(USER, "projects/a.md", "deadline")
        assert not (await _grep(svc, pattern="deadline", path="")).failed

    async def test_store_failure_is_unavailable(self):
        svc, store = _seeded()
        store.fail_next["list"] = [RuntimeError("boom")]
        r = await _grep(svc, pattern="x", path="")
        assert r.failed and "unavailable" in r.text

    async def test_file_deleted_between_list_and_read_is_skipped(self):
        svc, store = _seeded()
        real_read = store.read

        async def read(user_id, path, generation=None):
            if path == "projects/trips/packing.md":
                raise FileNotFoundError(path)
            return await real_read(user_id, path, generation)

        store.read = read
        r = await _grep(svc, pattern="deadline", path="projects/")
        assert not r.failed and r.text.splitlines()[1] == "searched 1 text file: projects/ (1)"


class TestLog:
    async def test_one_line_with_counts_and_no_pattern_or_content(self, caplog):
        svc, _ = _seeded()
        with caplog.at_level(logging.INFO):
            await _grep(svc, pattern="secretword deadline", path="projects/")
        recs = [r.getMessage() for r in caplog.records if "[Workspace]" in r.getMessage()]
        assert len(recs) == 1
        assert recs[0].startswith("[Workspace] op=grep ") and "path=projects/ " in recs[0]
        assert "files=2 matches=0" in recs[0] and "result=ok" in recs[0]
        assert "secretword" not in recs[0]

    async def test_other_commands_keep_their_format(self, caplog):
        svc, _ = _seeded()
        with caplog.at_level(logging.INFO):
            await svc.run(_call(), "view", {"path": "projects/"})
        rec = [r.getMessage() for r in caplog.records if "[Workspace]" in r.getMessage()][0]
        assert "files=" not in rec


class TestTreeView:
    async def test_view_folder_is_a_tree_and_depth_is_passed(self):
        svc, store = _seeded()
        store.seed(USER, "projects/trips/a/b/c.md", "x")
        r = await svc.run(_call(), "view", {"path": "projects/", "depth": "1"})
        assert r.text.splitlines()[:3] == ["workspace:projects/ — 3 files", "trips/ · 2 files", "  a/ · 1 file · 1 B"]
        assert "(deeper folders are collapsed — view that folder to open it)" in r.text
        assert store.calls == [("list", "projects/")]

    async def test_view_empty_path_is_the_whole_tree(self):
        svc, _ = _seeded()
        r = await svc.run(_call(), "view", {"path": ""})
        assert r.text.splitlines()[0] == "workspace (all folders) — 5 files"
