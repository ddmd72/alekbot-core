"""WorkspaceService — insert, grep generations, the long-file outline and the size tails (AGENT_WORKSPACE_RFC D18)."""
import logging

from src.domain.workspace import VIEW_FULL_MAX_CHARS, WorkspaceCall
from src.services.workspace_service import WorkspaceService
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

USER = "u1"
P = "projects/garden-shed.md"


def _svc():
    store = InMemoryWorkspaceStore()
    return WorkspaceService(store), store


def _call(known=None):
    return WorkspaceCall(user_id=USER, origin="chat", known=dict(known or {}))


STALE = (f"SYSTEM: {P} changed since you last read it in this run (or you have not read it): "
         "view the lines around {n} again, then insert.")


class TestInsertAtTheEnd:
    async def test_needs_no_prior_read_and_reports_size(self, caplog):
        svc, store = _svc()
        e = store.seed(USER, P, "decision expected 2026-10-30\n")
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "received 2026-10-28"})
        assert not r.failed
        now = store.versions_of(USER, P)[-1]
        assert now.generation > e.generation
        assert store.live_text(USER, P) == "decision expected 2026-10-30\nreceived 2026-10-28\n"
        assert r.text == (f"inserted 1 line at line 2 of workspace:{P} · generation {now.generation} · "
                          "file now 2 lines · 49 B")
        assert dict(r.generations) == {P: now.generation}
        assert "op=insert" in caplog.text and "result=ok" in caplog.text

    async def test_the_text_is_not_echoed(self):
        svc, store = _svc()
        store.seed(USER, P, "a\n")
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "secret-entry-text"})
        assert "secret-entry-text" not in r.text

    async def test_several_lines_say_where_they_landed(self):
        svc, store = _svc()
        store.seed(USER, P, "a\n")
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": "-1", "insert_text": "b\nc\n"})
        assert "inserted 2 lines at lines 2–3 of" in r.text and r.text.endswith("file now 3 lines · 6 B")

    async def test_zero_needs_no_prior_read(self):
        svc, store = _svc()
        store.seed(USER, P, "b\n")
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": 0, "insert_text": "a"})
        assert not r.failed and store.live_text(USER, P) == "a\nb\n"

    async def test_a_412_is_reapplied_once(self):
        svc, store = _svc()
        store.seed(USER, P, "a\n")
        store.before_put.append(lambda: store.seed(USER, P, "a\nother writer\n"))
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "mine"})
        assert not r.failed and store.live_text(USER, P) == "a\nother writer\nmine\n"

    async def test_a_second_412_is_a_conflict(self):
        svc, store = _svc()
        store.seed(USER, P, "a\n")
        store.before_put += [lambda: store.seed(USER, P, "a\n1\n"), lambda: store.seed(USER, P, "a\n1\n2\n")]
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "mine"})
        assert r.failed and r.text.startswith(f"SYSTEM: {P} changed since you read it")


class TestInsertInTheMiddle:
    async def test_without_a_read_is_stale(self, caplog):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "insert", {"path": P, "insert_line": 1, "insert_text": "x"})
        assert r.failed and r.text == STALE.format(n=1)
        assert store.live_text(USER, P) == "a\nb\n"
        assert "result=refused:stale" in caplog.text

    async def test_after_a_view_it_is_applied(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        seen = await svc.run(_call(), "view", {"path": P})
        r = await svc.run(_call(seen.generations), "insert", {"path": P, "insert_line": 1, "insert_text": "x"})
        assert not r.failed and store.live_text(USER, P) == "a\nx\nb\n"

    async def test_after_a_grep_it_is_applied(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nneedle\nb\n")
        found = await svc.run(_call(), "grep", {"pattern": "needle", "path": P})
        gen = store.versions_of(USER, P)[-1].generation
        assert dict(found.line_generations) == {P: gen} and not found.generations
        r = await svc.run(WorkspaceCall(user_id=USER, origin="chat", line_known=found.line_generations), "insert", {"path": P, "insert_line": 2, "insert_text": "x"})
        assert not r.failed and store.live_text(USER, P) == "a\nneedle\nx\nb\n"

    async def test_another_writer_between_view_and_insert_is_stale(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        seen = await svc.run(_call(), "view", {"path": P})
        store.seed(USER, P, "a\nb\nc\n")
        r = await svc.run(_call(seen.generations), "insert", {"path": P, "insert_line": 1, "insert_text": "x"})
        assert r.failed and r.text == STALE.format(n=1)

    async def test_a_412_in_the_middle_is_stale_not_reapplied(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        seen = await svc.run(_call(), "view", {"path": P})
        store.before_put.append(lambda: store.seed(USER, P, "z\na\nb\n"))
        r = await svc.run(_call(seen.generations), "insert", {"path": P, "insert_line": 1, "insert_text": "x"})
        assert r.failed and r.text == STALE.format(n=1)
        assert store.live_text(USER, P) == "z\na\nb\n"


class TestInsertRefusals:
    async def test_out_of_range(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        seen = await svc.run(_call(), "view", {"path": P})   # the current generation is known: the range check is reached
        r = await svc.run(_call(seen.generations), "insert", {"path": P, "insert_line": 5, "insert_text": "x"})
        assert r.failed and r.text == (
            "SYSTEM: insert_line 5 is out of range: the file has 2 lines; use 0..2, or -1 for the end.")
        assert store.live_text(USER, P) == "a\nb\n"

    async def test_an_unread_out_of_range_middle_line_is_reported_stale_first(self, caplog):
        svc, store = _svc()
        store.seed(USER, P, "a\nb\n")
        with caplog.at_level(logging.INFO):
            r = await svc.run(_call(), "insert", {"path": P, "insert_line": 5, "insert_text": "x"})
        assert r.failed and r.text == STALE.format(n=5)
        assert store.live_text(USER, P) == "a\nb\n"
        assert "result=refused:stale" in caplog.text

    async def test_missing_file(self):
        svc, _ = _svc()
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "x"})
        assert r.failed and r.text == f"SYSTEM: {P} does not exist; view it to see its history."

    async def test_empty_text_and_missing_line(self):
        svc, store = _svc()
        store.seed(USER, P, "a\n")
        assert (await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": ""})).failed
        r = await svc.run(_call(), "insert", {"path": P, "insert_text": "x"})
        assert r.failed and "insert needs insert_line" in r.text

    async def test_same_folders_and_kinds_as_str_replace(self):
        svc, _ = _svc()
        r1 = await svc.run(_call(), "insert", {"path": "binary_files/a.pdf", "insert_line": -1, "insert_text": "x"})
        r2 = await svc.run(_call(), "insert", {"path": "projects/a.pdf", "insert_line": -1, "insert_text": "x"})
        assert "binary_files/ holds binary files only" in r1.text and "insert writes text files only" in r2.text

    async def test_over_the_text_cap_names_the_fix(self):
        svc, store = _svc()
        store.seed(USER, P, "a" * (3 * 1024 * 1024 - 1))
        r = await svc.run(_call(), "insert", {"path": P, "insert_line": -1, "insert_text": "bb"})
        assert r.failed and "rename it to an archive name (e.g. projects/garden-shed-archive-1.md)" in r.text


class TestLongFileView:
    @staticmethod
    def _long():
        body = "".join(f"entry {i}: " + "x" * 90 + "\n" for i in range(VIEW_FULL_MAX_CHARS // 100 + 10))
        return "# Garden shed\n" + body + "## Neighbours\nKim and Jordan\n"

    async def test_no_range_shows_the_outline_and_records_the_generation(self):
        svc, store = _svc()
        e = store.seed(USER, P, self._long())
        r = await svc.run(_call(), "view", {"path": P})
        assert not r.failed and dict(r.generations) == {P: e.generation}
        assert "     1\t# Garden shed" in r.text and "## Neighbours" in r.text
        assert "entry 5:" not in r.text and "text not shown" in r.text
        assert r.text.startswith(f"[workspace:{P} · generation {e.generation} · ")

    async def test_an_insert_after_a_heading_passes_the_guard(self):
        svc, store = _svc()
        store.seed(USER, P, self._long())
        seen = await svc.run(_call(), "view", {"path": P})
        r = await svc.run(_call(seen.generations), "insert", {"path": P, "insert_line": 1, "insert_text": "intro"})
        assert not r.failed

    async def test_any_explicit_offset_or_limit_shows_text(self):
        svc, store = _svc()
        store.seed(USER, P, self._long())
        for args in ({"offset": 2}, {"limit": 3}, {"offset": -3}):
            r = await svc.run(_call(), "view", {"path": P, **args})
            assert "entry " in r.text and "text not shown" not in r.text

    async def test_a_file_under_the_threshold_shows_its_text(self):
        svc, store = _svc()
        store.seed(USER, P, "# Short\n" + "y" * (VIEW_FULL_MAX_CHARS - 20))
        r = await svc.run(_call(), "view", {"path": P})
        assert "# Short" in r.text and "yyyy" in r.text and "text not shown" not in r.text

    async def test_no_headings_shows_only_the_hint(self):
        svc, store = _svc()
        store.seed(USER, P, ("z" * 99 + "\n") * 200)
        r = await svc.run(_call(), "view", {"path": P})
        assert r.text.splitlines()[-1].startswith("(long file: 200 lines,") and "zzz" not in r.text

    async def test_an_old_generation_follows_the_same_rule_and_is_not_recorded(self):
        svc, store = _svc()
        old = store.seed(USER, P, self._long())
        store.seed(USER, P, "short\n")
        r = await svc.run(_call(), "view", {"path": P, "generation": str(old.generation)})
        assert "text not shown" in r.text and not r.generations

    async def test_tail_read_with_a_negative_offset(self):
        svc, store = _svc()
        store.seed(USER, P, "\n".join(f"row {i}" for i in range(1, 101)) + "\n")
        r = await svc.run(_call(), "view", {"path": P, "offset": -2})
        assert r.text.splitlines()[-2:] == ["    99\trow 99", "   100\trow 100"] and "row 98" not in r.text


class TestGrepIsNotAView:
    async def test_grep_then_create_overwrite_is_still_refused(self):
        svc, store = _svc()
        store.seed(USER, P, "needle\n")
        found = await svc.run(_call(), "grep", {"pattern": "needle", "path": P})
        r = await svc.run(
            WorkspaceCall(user_id=USER, origin="chat", known=found.generations, line_known=found.line_generations),
            "create", {"path": P, "content": "lost"})
        assert r.failed and "exists" in r.text and store.live_text(USER, P) == "needle\n"

    async def test_line_numbers_of_a_changed_file_are_stale(self):
        svc, store = _svc()
        store.seed(USER, P, "a\nneedle\n")
        found = await svc.run(_call(), "grep", {"pattern": "needle", "path": P})
        store.seed(USER, P, "z\na\nneedle\n")
        r = await svc.run(WorkspaceCall(user_id=USER, origin="chat", line_known=found.line_generations), "insert",
                          {"path": P, "insert_line": 2, "insert_text": "x"})
        assert r.failed and r.text == STALE.format(n=2)

    async def test_grep_then_view_of_a_same_fire_file_still_shows_the_b1_note(self):
        svc, store = _svc()
        fire = "reminder:1729401234567@2026-10-20T06:00:00+00:00"
        store.seed(USER, P, "old\n")
        store.seed(USER, P, "needle\n", fire=fire)
        found = await svc.run(_call(), "grep", {"pattern": "needle", "path": P})
        call = WorkspaceCall(user_id=USER, origin="chat", fire_key=fire, known=found.generations,
                             line_known=found.line_generations)
        r = await svc.run(call, "view", {"path": P})
        assert "written by an earlier attempt of this same fire" in r.text


class TestResultTails:
    async def test_create_and_str_replace_say_how_big_the_file_is(self):
        svc, store = _svc()
        r = await svc.run(_call(), "create", {"path": P, "content": "a\nb\n"})
        assert r.text.endswith(" · 2 lines · 4 B")
        seen = await svc.run(_call(), "view", {"path": P})
        r = await svc.run(_call(seen.generations), "str_replace", {"path": P, "old_str": "b", "new_str": "bb\ncc"})
        assert " · file now 3 lines · 8 B\n" in r.text and r.text.startswith(f"replaced in workspace:{P} · generation ")
        again = await svc.run(_call(r.generations), "create", {"path": P, "content": "z"})
        assert again.text.endswith(" · 1 line · 1 B")

    async def test_multi_match_lists_the_lines(self):
        svc, store = _svc()
        store.seed(USER, P, "x\nKim\ny\nKim\n")
        r = await svc.run(_call(), "str_replace", {"path": P, "old_str": "Kim", "new_str": "Jordan"})
        assert r.text == f"SYSTEM: found 2 matches of old_str in {P} at lines 2, 4; include more surrounding text."
