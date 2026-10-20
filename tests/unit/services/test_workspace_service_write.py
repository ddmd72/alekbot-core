"""WorkspaceService — create and str_replace (AGENT_WORKSPACE_RFC §6, §7.3, §7.5, §16 had_history)."""
import logging

from src.domain.workspace import (
    MAX_BYTES_PER_USER,
    MAX_FILES_PER_USER,
    MAX_TEXT_FILE_BYTES,
    WorkspaceCall,
    WorkspaceDeleteMeta,
)
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call(known=None, origin="chat", fire=None):
    return WorkspaceCall(user_id=USER, origin=origin, fire_key=fire, known=dict(known or {}))


class TestCreate:
    async def test_new_path(self, caplog):
        svc, store = _svc()
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(origin="background:execute_reminder", fire=F), "create",
                              {"path": P, "content": "# A\nB"})
        [e] = store.versions_of(USER, P)
        assert not r.failed and r.text == f"created workspace:{P} · generation {e.generation} · 2 lines"
        assert dict(r.generations) == {P: e.generation}
        assert (e.origin, e.fire) == ("background:execute_reminder", F)
        assert store.calls == [("list", ""), ("list", P), ("put", P)]
        assert "op=create" in caplog.text and "had_history=0" in caplog.text

    async def test_had_history_when_the_path_was_deleted(self, caplog):
        svc, store = _svc()
        e = store.seed(USER, P, "old")
        await store.delete(USER, P, e.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "create", {"path": P, "content": "new"})
        assert not r.failed and "had_history=1" in caplog.text

    async def test_existing_path_without_a_view_is_refused(self, caplog):
        svc, store = _svc()
        store.seed(USER, P, "keep")
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "create", {"path": P, "content": "lost"})
        assert r.failed and r.text == (
            f"SYSTEM: {P} exists. View it first and use str_replace, or view it and create again to overwrite.")
        assert store.live_text(USER, P) == "keep"
        assert any(rec.levelno == logging.WARNING and "result=refused:exists" in rec.getMessage()
                   for rec in caplog.records)

    async def test_overwrite_after_a_view_in_this_execution(self):
        svc, store = _svc()
        e = store.seed(USER, P, "old")
        store.calls.clear()
        r = await svc.run(_call(known={P: e.generation}), "create", {"path": P, "content": "new"})
        assert r.text.startswith(f"overwritten workspace:{P} · generation ") and r.text.endswith(" · 1 line")
        assert store.live_text(USER, P) == "new"
        assert store.calls == [("put", P)]  # no cap check for an overwrite

    async def test_overwrite_after_someone_else_wrote_is_a_conflict(self):
        svc, store = _svc()
        e = store.seed(USER, P, "old")
        store.seed(USER, P, "theirs")
        r = await svc.run(_call(known={P: e.generation}), "create", {"path": P, "content": "mine"})
        assert r.failed and r.text == (f"SYSTEM: {P} changed since you read it (another conversation or run wrote "
                                       "it). View it again and redo your change.")
        assert store.live_text(USER, P) == "theirs"

    async def test_files_folder_is_copy_only(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "create", {"path": "files/a.md", "content": "x"})
        assert r.text == "SYSTEM: invalid workspace path 'files/a.md': files/ holds copies only; use copy."

    async def test_text_kinds_only(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "create", {"path": "projects/a.pdf", "content": "x"})
        assert r.text == ("SYSTEM: invalid workspace path 'projects/a.pdf': create writes text files only "
                          "(.md .txt .csv .tsv .json .yaml .yml).")

    async def test_text_size(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "create", {"path": P, "content": "x" * (MAX_TEXT_FILE_BYTES + 1)})
        assert r.text == f"SYSTEM: {P}: text file over 256 KB."

    async def test_file_cap(self):
        svc, store = _svc()
        for i in range(MAX_FILES_PER_USER):
            store.seed(USER, f"projects/f{i}.md", "x")
        r = await svc.run(_call(), "create", {"path": P, "content": "x"})
        assert r.text == "SYSTEM: workspace full (100 files); delete or merge first."

    async def test_byte_cap_counts_the_new_size(self):
        svc, store = _svc()
        store.seed(USER, "files/big.pdf", b"x", size=MAX_BYTES_PER_USER - 3)
        assert not (await svc.run(_call(), "create", {"path": "projects/ok.md", "content": "abc"})).failed
        r = await svc.run(_call(), "create", {"path": P, "content": "d"})
        assert r.text == "SYSTEM: workspace full (100 MB); delete or merge first."

    async def test_empty_content_is_an_empty_file(self):
        svc, store = _svc()
        r = await svc.run(_call(), "create", {"path": P})
        assert not r.failed and r.text.endswith(" · 0 lines") and store.live_text(USER, P) == ""


class TestStrReplace:
    async def test_replaces_without_a_prior_view(self):
        svc, store = _svc()
        store.seed(USER, P, "A: 1\nB: 2")
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "B: 2", "new_str": "B: 3"})
        e = store.versions_of(USER, P)[-1]
        assert r.text == f"replaced in workspace:{P} · generation {e.generation}\n     2\tB: 3"
        assert dict(r.generations) == {P: e.generation}
        assert store.live_text(USER, P) == "A: 1\nB: 3"

    async def test_reapplies_once_after_a_concurrent_write(self):
        svc, store = _svc()
        store.seed(USER, P, "A: 1\nB: 2")
        store.before_put.append(lambda: store.seed(USER, P, "A: 9\nB: 2"))
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "B: 2", "new_str": "B: 3"})
        assert not r.failed and store.live_text(USER, P) == "A: 9\nB: 3"
        assert store.calls == [("read", P), ("put", P), ("read", P), ("put", P)]

    async def test_second_conflict_is_reported(self):
        svc, store = _svc()
        store.seed(USER, P, "A: 1\nB: 2")
        store.before_put += [lambda: store.seed(USER, P, "A: 8\nB: 2"), lambda: store.seed(USER, P, "A: 9\nB: 2")]
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "B: 2", "new_str": "B: 3"})
        assert r.failed and "changed since you read it" in r.text
        assert store.live_text(USER, P) == "A: 9\nB: 2"

    async def test_no_match_and_several_matches(self):
        svc, store = _svc()
        store.seed(USER, P, "x\nx")
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "y", "new_str": "z"})
        assert r.text == f"SYSTEM: old_str not found in {P}."
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "x", "new_str": "z"})
        assert r.text == f"SYSTEM: found 2 matches of old_str in {P}; include more surrounding text."

    async def test_missing_file(self, caplog):
        svc, _ = _svc()
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "a", "new_str": "b"})
        assert r.failed and r.text == f"SYSTEM: {P} does not exist; view it to see its history."
        assert any(rec.levelno == logging.WARNING and "result=not_found" in rec.getMessage()
                   for rec in caplog.records)

    async def test_files_folder_refused(self):
        svc, store = _svc()
        store.seed(USER, "files/a.txt", "x")
        r = await svc.run(_call(), "str_replace", {"path": "files/a.txt", "old_str": "x", "new_str": "y"})
        assert r.failed and "files/ holds copies only" in r.text

    async def test_new_str_may_be_empty(self):
        svc, store = _svc()
        store.seed(USER, P, "keep drop")
        await svc.run(_call(), "str_replace", {"path": P, "old_str": " drop", "new_str": ""})
        assert store.live_text(USER, P) == "keep"
