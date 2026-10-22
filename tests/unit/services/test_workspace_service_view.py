"""WorkspaceService — view, history, the log line and failures (AGENT_WORKSPACE_RFC §7.3–§7.5, §16)."""
import logging
import re

from src.domain.workspace import WorkspaceCall, WorkspaceDeleteMeta, render_same_fire_line
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/a.md"
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"
EARLIER = "reminder:1729401234567@2026-10-19T06:00:00+00:00"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call(known=None, origin="chat", fire=None):
    return WorkspaceCall(user_id=USER, origin=origin, fire_key=fire, known=dict(known or {}))


class TestViewFile:
    async def test_header_numbered_lines_and_recorded_generation(self):
        svc, store = _svc()
        e = store.seed(USER, P, "# A\nline two")
        r = await svc.run(_call(), "view", {"path": "workspace:projects/a.md"})
        assert not r.failed
        assert r.text.startswith(f"[workspace:{P} · generation {e.generation} · 2 lines · 12 B\n last written ")
        assert " by chat]\n     1\t# A\n     2\tline two" in r.text  # neutral header, then the lines (D17)
        assert dict(r.generations) == {P: e.generation}
        assert store.calls == [("read", P)]

    async def test_window(self):
        svc, store = _svc()
        store.seed(USER, P, "1\n2\n3\n4\n5")
        r = await svc.run(_call(), "view", {"path": P, "offset": 2, "limit": "2"})
        assert r.text.endswith("     2\t2\n     3\t3\n…lines 2–3 of 5; view with offset=4 to read on")

    async def test_binary_is_refused_but_recorded(self):
        svc, store = _svc()
        e = store.seed(USER, "files/lease.pdf", b"%PDF-1.4")
        r = await svc.run(_call(), "view", {"path": "files/lease.pdf"})
        assert r.failed
        assert r.text == ("SYSTEM: files/lease.pdf is binary; open it with open_file and file_ref "
                          "workspace:files/lease.pdf.")
        assert dict(r.generations) == {"files/lease.pdf": e.generation}
        assert store.calls == [("list", "files/lease.pdf")]  # metadata only, no download for a refusal

    async def test_text_extension_that_is_not_utf8_is_binary(self):
        svc, store = _svc()
        store.seed(USER, "files/a.txt", b"\xff\xfe")
        r = await svc.run(_call(), "view", {"path": "files/a.txt"})
        assert r.failed and "is binary" in r.text

    async def test_wrong_store(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "view", {"path": "drive:01ABC"})
        assert r.failed and "work_with_drive_files" in r.text


class TestViewVersion:
    async def test_old_version_is_read_only(self):
        svc, store = _svc()
        old = store.seed(USER, P, "v1")
        store.seed(USER, P, "v2")
        r = await svc.run(_call(), "view", {"path": P, "generation": str(old.generation)})
        assert f"· version {old.generation}, not current ·" in r.text and "     1\tv1" in r.text
        assert dict(r.generations) == {}

    async def test_unknown_generation(self):
        svc, store = _svc()
        store.seed(USER, P, "v1")
        r = await svc.run(_call(), "view", {"path": P, "generation": "999"})
        assert r.failed and r.text == f"SYSTEM: {P} has no version 999; history lists them."


class TestSameFire:
    async def test_same_fire_names_the_generation_from_before_the_fire(self):
        svc, store = _svc()
        before = store.seed(USER, P, "covered: x", origin="background:execute_reminder", fire=EARLIER)
        store.seed(USER, P, "covered: x, y", origin="background:execute_reminder", fire=F)
        r = await svc.run(_call(origin="background:execute_reminder", fire=F), "view", {"path": P})
        assert render_same_fire_line(before.generation) in r.text
        assert store.calls == [("read", P), ("list", P)]

    async def test_same_fire_when_the_file_did_not_exist_before(self):
        svc, store = _svc()
        store.seed(USER, P, "covered: y", origin="background:execute_reminder", fire=F)
        r = await svc.run(_call(origin="background:execute_reminder", fire=F), "view", {"path": P})
        assert render_same_fire_line(None) in r.text

    async def test_no_same_fire_line_for_a_version_this_execution_wrote_or_viewed(self):
        svc, store = _svc()
        store.seed(USER, P, "covered: x", origin="background:execute_reminder", fire=F)
        first = await svc.run(_call(origin="background:execute_reminder", fire=F), "view", {"path": P})
        assert "same fire" in first.text
        # the execution writes a new version in the same fire; the handler records its generation as known
        mine = store.seed(USER, P, "covered: x, y", origin="background:execute_reminder", fire=F)
        second = await svc.run(
            _call(known={P: mine.generation}, origin="background:execute_reminder", fire=F), "view", {"path": P})
        assert "same fire" not in second.text

    async def test_another_fire_gets_no_same_fire_line(self):
        svc, store = _svc()
        store.seed(USER, P, "covered: x", origin="background:execute_reminder", fire=EARLIER)
        r = await svc.run(_call(origin="background:execute_reminder", fire=F), "view", {"path": P})
        assert "same fire" not in r.text
        assert store.calls == [("read", P)]


class TestViewMissing:
    async def test_deleted_file_says_who_and_offers_restore(self):
        svc, store = _svc()
        e = store.seed(USER, P, "x")
        await store.delete(USER, P, e.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        r = await svc.run(_call(), "view", {"path": P})
        assert not r.failed
        assert r.text.startswith(f"workspace:{P} does not exist.\nIt was deleted ")
        assert "by chat. Versions kept:" in r.text and r.text.endswith("Do not recreate it.")

    async def test_never_existed(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "view", {"path": P})
        assert not r.failed and r.text == f"workspace:{P} does not exist and never did."


class TestViewFolder:
    async def test_lists_live_and_counts_deleted_in_one_call(self):
        svc, store = _svc()
        store.seed(USER, P, "a")
        gone = store.seed(USER, "projects/b.md", "b")
        await store.delete(USER, "projects/b.md", gone.generation, WorkspaceDeleteMeta(deleted_by="chat"))
        store.seed(USER, "references/r.md", "r")
        store.calls.clear()
        r = await svc.run(_call(), "view", {"path": "projects/"})
        assert r.text.startswith("workspace:projects/ — 1 file\n  projects/a.md · 1 B · ")
        assert r.text.endswith("(1 deleted file in this folder — history projects/ lists them)")
        assert store.calls == [("list", "projects/")]
        assert dict(r.generations) == {}

    async def test_everything(self):
        svc, store = _svc()
        store.seed(USER, P, "a")
        r = await svc.run(_call(), "view", {"path": ""})
        assert r.text.startswith("workspace (all folders) — 1 file")


class TestHistory:
    async def test_file(self):
        svc, store = _svc()
        store.seed(USER, P, "v1")
        store.seed(USER, P, "v2", origin="voice:tell_alek")
        r = await svc.run(_call(), "history", {"path": P})
        lines = r.text.splitlines()
        assert lines[0] == f"history of workspace:{P} — 2 versions"
        assert lines[1].endswith("voice:tell_alek · current") and "replaced" in lines[2]

    async def test_folder(self):
        svc, store = _svc()
        store.seed(USER, P, "v1")
        r = await svc.run(_call(), "history", {"path": "projects/"})
        assert r.text.startswith("history of workspace:projects/ — 1 file")


class TestLogAndFailures:
    async def test_one_log_line_per_command(self, caplog):
        svc, store = _svc()
        store.seed(USER, P, "x")
        with caplog.at_level(logging.INFO):
            await svc.run(_call(fire=F, origin="background:execute_reminder"), "view", {"path": P})
        lines = [r for r in caplog.records if "[Workspace]" in r.getMessage()]
        assert len(lines) == 1 and lines[0].levelno == logging.INFO
        assert re.fullmatch(
            rf"\[Workspace\] op=view user=u1 path={P} origin=background:execute_reminder "
            rf"fire={re.escape(F.removeprefix('reminder:'))} "
            r"had_history=- bytes=1 gen=\d+ result=ok ms=\d+",
            lines[0].getMessage(),
        )

    async def test_store_failure(self, caplog):
        svc, store = _svc()
        store.fail_next["read"] = [RuntimeError("gcs down")]
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "view", {"path": P})
        assert r.failed and r.text == "SYSTEM: the workspace is unavailable right now; nothing was changed."
        assert any(rec.levelno == logging.ERROR and "result=error" in rec.getMessage() for rec in caplog.records)

    async def test_unknown_command(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "grep", {"path": P})
        assert r.failed and r.text.startswith("SYSTEM: unknown command 'grep'; use one of: view, create,")

    async def test_refused_read_logs_at_info_with_the_reason(self, caplog):
        svc, _ = _svc()
        with caplog.at_level(logging.INFO):
            await svc.run(_call(), "view", {"path": "Bad Path.md"})
        [rec] = [r for r in caplog.records if "[Workspace]" in r.getMessage()]
        assert rec.levelno == logging.INFO
        assert "result=refused:wrong_store" in rec.getMessage() and "path=Bad_Path.md" in rec.getMessage()

    async def test_a_raw_path_is_one_capped_token(self, caplog):
        svc, _ = _svc()
        with caplog.at_level(logging.INFO):
            await svc.run(_call(), "view", {"path": "Bad\nPath " + "x" * 300})
        [rec] = [r for r in caplog.records if "[Workspace]" in r.getMessage()]
        token = rec.getMessage().split(" path=", 1)[1].split(" ", 1)[0]
        assert token.startswith("Bad_Path_x") and len(token) == 120 and "\n" not in rec.getMessage()
