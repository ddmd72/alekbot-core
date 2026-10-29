"""Models, caps, windows, str_replace and every text the workspace tool shows (AGENT_WORKSPACE_RFC §6, §7.3–§7.5,
§8.4)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.exceptions import WorkspaceRefused
from src.domain.workspace import (
    MAX_BYTES_PER_USER,
    MAX_FILES_PER_USER,
    MAX_FILE_BYTES,
    MAX_TEXT_FILE_BYTES,
    VIEW_MAX_BYTES,
    WorkspaceCall,
    WorkspaceEntry,
    WorkspaceReferences,
    WorkspaceResult,
    apply_str_replace,
    binary_message,
    changed_lines,
    check_caps,
    check_file_size,
    find_references,
    format_size,
    number_lines,
    prior_generation_before_fire,
    render_history,
    render_listing,
    render_missing,
    render_same_fire_line,
    render_view_header,
    view_window,
    window_note,
)

UTC = timezone.utc
F = "reminder:1729401234567@2026-10-20T06:00:00+00:00"
P = "projects/morning-briefing.md"


def _e(path=P, generation=101, size=2048, updated=datetime(2026, 10, 19, 9, 10, tzinfo=UTC), **kw):
    return WorkspaceEntry(path=path, generation=generation, size=size, updated=updated, **kw)


class TestSizes:
    @pytest.mark.parametrize("n,text", [(12, "12 B"), (1024, "1.0 KB"), (2150, "2.1 KB"), (3 * 1024 * 1024, "3.0 MB")])
    def test_format_size(self, n, text):
        assert format_size(n) == text


class TestCaps:
    def test_file_count(self):
        live = [_e(path=f"projects/f{i}.md", generation=i, size=1) for i in range(MAX_FILES_PER_USER)]
        with pytest.raises(WorkspaceRefused) as e:
            check_caps(live, 1)
        assert str(e.value) == "workspace full (100 files); delete or merge first"
        assert e.value.reason == "caps"

    def test_bytes_count_the_new_size(self):
        live = [_e(size=MAX_BYTES_PER_USER - 5)]
        check_caps(live, 5)
        with pytest.raises(WorkspaceRefused) as e:
            check_caps(live, 6)
        assert str(e.value) == "workspace full (100 MB); delete or merge first"

    def test_file_and_text_sizes(self):
        check_file_size("files/a.pdf", b"x" * MAX_FILE_BYTES)
        with pytest.raises(WorkspaceRefused) as e:
            check_file_size("files/a.pdf", b"x" * (MAX_FILE_BYTES + 1))
        assert str(e.value) == "files/a.pdf: file over 10 MB"
        with pytest.raises(WorkspaceRefused) as e:
            check_file_size("projects/a.md", b"x" * (MAX_TEXT_FILE_BYTES + 1))
        assert str(e.value) == "projects/a.md: text file over 256 KB"
        check_file_size("files/a.txt", b"\xff" * (MAX_TEXT_FILE_BYTES + 1))  # not UTF-8: binary, 10 MB cap


class TestWindows:
    @pytest.mark.parametrize("offset,limit", [(None, None), ("", ""), (0, 0), (-3, -1), ("0", "-2"), ("x", "y")])
    def test_defaults(self, offset, limit):
        assert view_window(offset, limit) == (1, 2000)

    def test_numbers_lines_like_cat_n(self):
        w = number_lines("# A\nsecond\nthird")
        assert w.text == "     1\t# A\n     2\tsecond\n     3\tthird"
        assert (w.first, w.last, w.total, w.cut) == (1, 3, 3, False)
        assert window_note(w) == ""

    def test_offset_and_limit(self):
        w = number_lines("a\nb\nc\nd\ne", "2", 2)
        assert w.text == "     2\tb\n     3\tc"
        assert w.cut
        assert window_note(w) == "…lines 2–3 of 5; view with offset=4 to read on"

    def test_long_line_cut(self):
        w = number_lines("x" * 2500)
        assert w.text.endswith(" …[line cut]")
        assert len(w.text) < 2100

    def test_byte_cap(self):
        w = number_lines("\n".join("y" * 1000 for _ in range(300)))
        assert len(w.text.encode("utf-8")) <= VIEW_MAX_BYTES
        assert w.cut

    def test_empty_and_past_the_end(self):
        empty = number_lines("")
        assert (empty.text, empty.total, empty.cut) == ("", 0, False)
        past = number_lines("a\nb", 5)
        assert past.text == "" and window_note(past) == "(the file has 2 lines; offset 5 is past the end)"


class TestStrReplace:
    def test_one_match(self):
        r = apply_str_replace(P, "A: 1\nB: 2\nC: 3", "B: 2", "B: 3\nB2: 4")
        assert r.text == "A: 1\nB: 3\nB2: 4\nC: 3"
        assert (r.first_line, r.line_count) == (2, 2)
        assert changed_lines(r) == "     2\tB: 3\n     3\tB2: 4"

    def test_none(self):
        with pytest.raises(WorkspaceRefused) as e:
            apply_str_replace(P, "A", "B", "C")
        assert str(e.value) == f"old_str not found in {P}" and e.value.reason == "no_match"

    def test_several(self):
        with pytest.raises(WorkspaceRefused) as e:
            apply_str_replace(P, "x\nx", "x", "y")
        assert str(e.value) == f"found 2 matches of old_str in {P}; include more surrounding text"
        assert e.value.reason == "multi_match"

    def test_empty_old_str(self):
        with pytest.raises(WorkspaceRefused):
            apply_str_replace(P, "x", "", "y")

    def test_empty_new_str_deletes(self):
        assert apply_str_replace(P, "keep drop", " drop", "").text == "keep"


class TestViewHeader:
    def test_reminder_write(self):
        e = _e(generation=101, size=2150, updated=datetime(2026, 10, 20, 6, 1, tzinfo=UTC),
               origin="background:execute_reminder", fire=F)
        assert render_view_header(e, 38) == (
            f"[workspace:{P} · generation 101 · 38 lines · 2.1 KB\n"
            f" last written 2026-10-20 06:01 UTC by background:execute_reminder (fire {F})]"
        )

    def test_chat_write_has_no_fire_and_no_framing(self):
        header = render_view_header(_e(origin="chat"), 3)
        assert "(fire" not in header and header.endswith(" by chat]")  # neutral (D17): no "data, not instructions"

    def test_old_version(self):
        assert "· version 101, not current ·" in render_view_header(_e(live=False), 3, version_view=True)


class TestSameFire:
    def test_line_with_prior(self):
        assert render_same_fire_line(101) == (
            "(this version was written by an earlier attempt of this same fire; its result may not have reached the "
            "user.\nThe version before this fire is generation 101.)"
        )

    def test_line_without_prior(self):
        assert render_same_fire_line(None) == (
            "(this version was written by an earlier attempt of this same fire; its result may not have reached the "
            "user.\nThe file did not exist before this fire.)"
        )

    def test_prior_is_the_newest_version_of_another_fire(self):
        versions = [_e(generation=101), _e(generation=102, fire="reminder:9@x"), _e(generation=103, fire=F),
                    _e(generation=104, fire=F)]
        assert prior_generation_before_fire(versions, F) == 102
        assert prior_generation_before_fire([_e(generation=103, fire=F)], F) is None


class TestListing:
    def test_folder(self):
        entries = [
            _e(path="projects/garden-shed.md", generation=110, size=4300, origin="chat",
               updated=datetime(2026, 10, 18, 8, tzinfo=UTC)),
            _e(path="projects/garden-shed.md", generation=105, size=10, live=False),
            _e(path=P, generation=111, size=2150, origin="background:execute_reminder",
               updated=datetime(2026, 10, 20, 6, tzinfo=UTC)),
            _e(path="projects/summer-trip.md", generation=104, size=1024, origin="voice:send_to_chat",
               updated=datetime(2026, 10, 2, 8, tzinfo=UTC)),
            _e(path="projects/old.md", generation=102, live=False),
            _e(path="projects/gone.md", generation=103, live=False),
        ]
        assert render_listing("projects/", entries) == (
            "workspace:projects/ — 3 files\n"
            "  projects/morning-briefing.md · 2.1 KB · 2026-10-20 · background:execute_reminder\n"
            "  projects/garden-shed.md · 4.2 KB · 2026-10-18 · chat\n"
            "  projects/summer-trip.md · 1.0 KB · 2026-10-02 · voice:send_to_chat\n"
            "(2 deleted files in this folder — history projects/ lists them)"
        )

    def test_empty_and_whole_workspace(self):
        assert render_listing("projects/", []) == "workspace:projects/ — empty"
        assert render_listing("", [_e(origin="chat")]).startswith("workspace (all folders) — 1 file\n")


class TestMissing:
    def _deleted(self, **kw):
        return _e(generation=101, origin="chat", live=False,
                  deleted_at=datetime(2026, 10, 19, 14, 2, tzinfo=UTC), deleted_by="chat", **kw)

    def test_deleted(self):
        assert render_missing(P, [self._deleted()]) == (
            f"workspace:{P} does not exist.\n"
            "It was deleted 2026-10-19 14:02 UTC by chat. Versions kept:\n"
            "  101 · 2026-10-19 09:10 UTC · chat · 2.0 KB\n"
            "Tell the user it is missing and offer `restore` with one of these generations. Do not recreate it."
        )

    def test_renamed(self):
        text = render_missing(P, [self._deleted(renamed_to="projects/briefing.md")])
        assert (
            "It was renamed to projects/briefing.md on 2026-10-19 14:02 UTC by chat; update the reminder's "
            "workspace_file (manage_self_reminders, context={\"workspace_file\": \"projects/briefing.md\"}) or "
            "rename it back. Versions kept:"
        ) in text

    def test_renamed_with_unknown_deleted_at(self):
        text = render_missing(P, [_e(live=False, renamed_to="projects/briefing.md")])
        assert "It was renamed to projects/briefing.md at an unknown time; update the reminder's workspace_file" in text

    def test_never_existed(self):
        assert render_missing(P, []) == f"workspace:{P} does not exist and never did."

    def test_deleted_without_metadata(self):
        assert "It was deleted 2026-10-19 14:02 UTC. Versions kept:" in render_missing(
            P, [_e(live=False, deleted_at=datetime(2026, 10, 19, 14, 2, tzinfo=UTC))])


class TestHistory:
    def test_file(self):
        entries = [
            _e(generation=101, origin="background:execute_reminder", fire=F, live=False,
               deleted_at=datetime(2026, 10, 20, 7, tzinfo=UTC)),
            _e(generation=102, origin="chat", restored_from=101),
        ]
        text = render_history(P, entries)
        lines = text.splitlines()
        assert lines[0] == f"history of workspace:{P} — 2 versions"
        assert lines[1].startswith("  102 · ") and lines[1].endswith("chat · restored from 101 · current")
        assert f"· fire {F} · replaced 2026-10-20 07:00 UTC" in lines[2]

    def test_deleted_and_renamed_status(self):
        gone = _e(live=False, deleted_by="background:daily_email_review",
                  deleted_at=datetime(2026, 10, 19, 14, 2, tzinfo=UTC))
        assert "deleted by background:daily_email_review 2026-10-19 14:02 UTC" in render_history(P, [gone])
        moved = _e(live=False, deleted_by="chat", renamed_to="projects/b.md",
                   deleted_at=datetime(2026, 10, 19, 14, 2, tzinfo=UTC))
        assert "renamed to projects/b.md by chat" in render_history(P, [moved])

    def test_never_existed(self):
        assert render_history(P, []) == f"workspace:{P} has no history: it never existed."

    def test_folder_lists_deleted_files(self):
        entries = [_e(path="projects/a.md", generation=101, origin="chat"),
                   _e(path="projects/b.md", generation=102, live=False, deleted_by="chat",
                      deleted_at=datetime(2026, 10, 19, 14, 2, tzinfo=UTC))]
        text = render_history("projects/", entries)
        assert text.startswith("history of workspace:projects/ — 2 files")
        assert "projects/b.md · 1 version · deleted by chat" in text
        assert "projects/a.md · 1 version · current, 2.0 KB" in text


class TestReferences:
    refs = WorkspaceReferences(
        skills=(("morning-briefing", f"At the start, view `workspace:{P}`."), ("other", "nothing here")),
        notes=({"note_id": "1729401234567", "text": "Morning briefing", "workspace_file": P},
               {"note_id": "1", "text": "x", "workspace_file": None}),
    )

    def test_delete_warnings(self):
        assert find_references(P, self.refs) == [
            'Warning: skill "morning-briefing" names this path',
            'Warning: reminder "Morning briefing" [1729401234567] uses this workspace file; update it '
            '(manage_self_reminders, context={"workspace_file": "<new path>"}) or restore the file.',
        ]

    def test_rename_names_the_new_path(self):
        warning = find_references(P, self.refs, new_path="projects/b.md")[1]
        assert 'context={"workspace_file": "projects/b.md"}' in warning

    def test_no_match(self):
        assert find_references("projects/x.md", self.refs) == []


class TestCallAndResult:
    def test_defaults(self):
        call = WorkspaceCall(user_id="u1", origin="chat")
        assert call.fire_key is None and dict(call.known) == {} and call.references == WorkspaceReferences()
        result = WorkspaceResult(text="ok")
        assert not result.failed and dict(result.generations) == {}

    def test_binary_message(self):
        assert binary_message("files/lease.pdf") == (
            "files/lease.pdf is binary; open it with open_file and file_ref workspace:files/lease.pdf"
        )


def test_entry_defaults():
    e = _e()
    assert e.live and e.origin == "" and e.deleted_by is None and e.restored_from is None
    assert e.updated + timedelta(0) == datetime(2026, 10, 19, 9, 10, tzinfo=UTC)
