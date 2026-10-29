"""WorkspaceService — delete, rename, restore (AGENT_WORKSPACE_RFC §5.3, §6, §7.3, §8.4)."""
import logging

from src.domain.exceptions import WorkspaceConflict
from src.domain.workspace import MAX_FILES_PER_USER, WorkspaceCall, WorkspaceDeleteMeta, WorkspaceReferences
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
Q = "projects/b.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call(known=None, origin="chat", fire=None, references=None):
    return WorkspaceCall(user_id=USER, origin=origin, fire_key=fire, known=dict(known or {}),
                         references=references or WorkspaceReferences())


class TestDelete:
    async def test_without_a_view_reads_the_generation_and_stamps_who(self):
        svc, store = _svc()
        e = store.seed(USER, P, "x")
        store.calls.clear()
        r = await svc.run(_call(origin="background:execute_reminder", fire=F), "delete", {"path": P})
        assert r.text == f"deleted workspace:{P}; restorable from history (generation {e.generation})"
        assert dict(r.generations) == {P: None}
        [v] = store.versions_of(USER, P)
        assert (v.live, v.deleted_by, v.deleted_fire) == (False, "background:execute_reminder", F)
        assert store.calls == [("list", P), ("delete", P)]

    async def test_with_a_view_uses_the_session_generation(self):
        svc, store = _svc()
        e = store.seed(USER, P, "x")
        store.calls.clear()
        await svc.run(_call(known={P: e.generation}), "delete", {"path": P})
        assert store.calls == [("delete", P)]

    async def test_stale_session_generation_is_a_conflict(self):
        svc, store = _svc()
        e = store.seed(USER, P, "x")
        store.seed(USER, P, "y")
        r = await svc.run(_call(known={P: e.generation}), "delete", {"path": P})
        assert r.failed and "changed since you read it" in r.text and store.live_text(USER, P) == "y"

    async def test_missing(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "delete", {"path": P})
        assert r.failed and r.text == f"SYSTEM: {P} does not exist; view it to see its history."

    async def test_warns_about_skills_and_reminders_that_name_it(self):
        svc, store = _svc()
        store.seed(USER, P, "x")
        refs = WorkspaceReferences(
            skills=(("morning-briefing", f"At the start, view `workspace:{P}`."),),
            notes=({"note_id": "1729401234567", "text": "Morning briefing", "workspace_file": P},),
        )
        r = await svc.run(_call(references=refs), "delete", {"path": P})
        assert r.text.splitlines()[1:] == [
            'Warning: skill "morning-briefing" names this path',
            'Warning: reminder "Morning briefing" [1729401234567] uses this workspace file; update it '
            '(manage_self_reminders, context={"workspace_file": "<new path>"}) or restore the file.',
        ]
        assert store.live_text(USER, P) is None  # the command still runs; history keeps the version


class TestRename:
    async def test_moves_and_records_where_it_went(self):
        svc, store = _svc()
        store.seed(USER, P, "state")
        r = await svc.run(_call(origin="voice:send_to_chat"), "rename", {"path": P, "new_path": Q})
        [dst] = store.versions_of(USER, Q)
        assert r.text == f"renamed workspace:{P} to workspace:{Q} · generation {dst.generation}"
        assert dict(r.generations) == {P: None, Q: dst.generation}
        assert store.live_text(USER, Q) == "state" and dst.origin == "voice:send_to_chat"
        [src] = store.versions_of(USER, P)
        assert (src.live, src.renamed_to, src.deleted_by) == (False, Q, "voice:send_to_chat")

    async def test_rename_is_not_cap_checked(self):
        svc, store = _svc()
        for i in range(MAX_FILES_PER_USER - 1):
            store.seed(USER, f"projects/f{i}.md", "x")
        store.seed(USER, P, "x")
        r = await svc.run(_call(), "rename", {"path": P, "new_path": Q})
        assert not r.failed and ("list", "") not in store.calls

    async def test_destination_exists(self):
        svc, store = _svc()
        store.seed(USER, P, "a")
        store.seed(USER, Q, "b")
        r = await svc.run(_call(), "rename", {"path": P, "new_path": Q})
        assert r.text == f"SYSTEM: {Q} already exists; pick another new_path, or view and delete it first."
        assert store.live_text(USER, P) == "a"

    async def test_folder_rules(self):
        svc, store = _svc()
        store.seed(USER, "files/lease.pdf", b"%PDF")
        store.seed(USER, P, "x")
        r = await svc.run(_call(), "rename", {"path": "files/lease.pdf", "new_path": "projects/lease.pdf"})
        assert r.failed and "a file in files/ stays in files/" in r.text
        r = await svc.run(_call(), "rename", {"path": P, "new_path": "files/a.md"})
        assert r.failed and "a file may not move into files/" in r.text

    async def test_source_delete_conflict_rolls_back(self):
        svc, store = _svc()
        store.seed(USER, P, "x")
        store.fail_next["delete"] = [WorkspaceConflict(P)]
        r = await svc.run(_call(), "rename", {"path": P, "new_path": Q})
        assert r.failed and r.text.startswith(f"SYSTEM: {P} changed since you read it")
        assert store.live_text(USER, P) == "x" and store.live_text(USER, Q) is None

    async def test_failed_rollback_names_both_paths(self, caplog):
        svc, store = _svc()
        store.seed(USER, P, "x")
        store.fail_next["delete"] = [WorkspaceConflict(P), RuntimeError("gcs down")]
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "rename", {"path": P, "new_path": Q})
        assert r.failed and f"both workspace:{P} and workspace:{Q} exist now" in r.text
        assert any(rec.levelno == logging.ERROR and "rename rollback failed" in rec.getMessage()
                   for rec in caplog.records)
        assert any(rec.levelno == logging.ERROR and "op=rename" in rec.getMessage()
                   and "result=error" in rec.getMessage() for rec in caplog.records)

    async def test_warning_names_the_new_path(self):
        svc, store = _svc()
        store.seed(USER, P, "x")
        refs = WorkspaceReferences(notes=({"note_id": "1", "text": "News", "workspace_file": P},))
        r = await svc.run(_call(references=refs), "rename", {"path": P, "new_path": Q})
        assert f'context={{"workspace_file": "{Q}"}}' in r.text


class TestRestore:
    async def test_brings_back_a_deleted_file(self):
        svc, store = _svc()
        old = store.seed(USER, P, "v1")
        await store.delete(USER, P, old.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        store.calls.clear()
        r = await svc.run(_call(), "restore", {"path": P, "generation": str(old.generation)})
        new = store.versions_of(USER, P)[-1]
        assert r.text == f"restored generation {old.generation} as generation {new.generation} of workspace:{P}"
        assert (new.live, new.restored_from, new.origin) == (True, old.generation, "chat")
        assert store.live_text(USER, P) == "v1"
        assert store.calls == [("read", P), ("list", ""), ("put", P)]

    async def test_over_a_live_file_needs_no_cap(self):
        svc, store = _svc()
        old = store.seed(USER, P, "v1")
        for i in range(MAX_FILES_PER_USER - 1):
            store.seed(USER, f"projects/f{i}.md", "x")
        store.seed(USER, P, "v2")
        r = await svc.run(_call(), "restore", {"path": P, "generation": old.generation})
        assert not r.failed and store.live_text(USER, P) == "v1"

    async def test_deleted_file_into_a_full_workspace(self):
        svc, store = _svc()
        old = store.seed(USER, P, "v1")
        await store.delete(USER, P, old.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        for i in range(MAX_FILES_PER_USER):
            store.seed(USER, f"projects/f{i}.md", "x")
        r = await svc.run(_call(), "restore", {"path": P, "generation": str(old.generation)})
        assert r.text == "SYSTEM: workspace full (100 files); delete or merge first."

    async def test_current_generation(self):
        svc, store = _svc()
        e = store.seed(USER, P, "v1")
        r = await svc.run(_call(), "restore", {"path": P, "generation": str(e.generation)})
        assert not r.failed and "already the current version" in r.text

    async def test_needs_a_known_generation(self):
        svc, store = _svc()
        store.seed(USER, P, "v1")
        r = await svc.run(_call(), "restore", {"path": P})
        assert r.text == "SYSTEM: restore needs a generation; history lists them."
        r = await svc.run(_call(), "restore", {"path": P, "generation": "999"})
        assert r.text == f"SYSTEM: {P} has no version 999; history lists them."
