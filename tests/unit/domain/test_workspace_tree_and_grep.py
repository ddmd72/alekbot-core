"""Folder view as a tree and the grep renderer (AGENT_WORKSPACE_RFC §7.4)."""
from datetime import datetime, timezone

import pytest

import src.domain.workspace as ws
from src.domain.workspace import (
    GREP_MAX_MATCHES,
    GREP_MAX_OUTPUT_BYTES,
    GREP_TRAILER,
    VIEW_MAX_LINE_CHARS,
    WorkspaceEntry,
    parse_context,
    parse_depth,
    parse_flag,
    render_grep,
    render_listing,
)

DAY = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc)


def _e(path, size=1024, live=True, origin="chat"):
    return WorkspaceEntry(path=path, generation=1, size=size, updated=DAY, origin=origin, live=live)


def _contains(needle):
    return lambda line: needle in line


class TestTree:
    def test_nesting_order_and_counts(self):
        entries = [
            _e("references/z.md"), _e("projects/b.md"), _e("projects/a.md"),
            _e("projects/trips/packing.md"), _e("projects/trips/list.md"),
        ]
        assert render_listing("", entries) == "\n".join([
            "workspace (all folders) — 5 files",
            "projects/ · 4 files",
            "  trips/ · 2 files",
            "    list.md · 1.0 KB · 2026-10-20 · chat",
            "    packing.md · 1.0 KB · 2026-10-20 · chat",
            "  a.md · 1.0 KB · 2026-10-20 · chat",
            "  b.md · 1.0 KB · 2026-10-20 · chat",
            "references/ · 1 file",
            "  z.md · 1.0 KB · 2026-10-20 · chat",
        ])

    def test_viewed_prefix_is_level_zero(self):
        text = render_listing("projects/", [_e("projects/trips/list.md"), _e("projects/a.md")])
        assert text.splitlines() == [
            "workspace:projects/ — 2 files",
            "trips/ · 1 file",
            "  list.md · 1.0 KB · 2026-10-20 · chat",
            "a.md · 1.0 KB · 2026-10-20 · chat",
        ]

    def test_depth_collapses_deeper_folders_with_a_hint(self):
        entries = [_e("projects/a/b/c/d.md", size=2048), _e("projects/a/b/c/e.md", size=2048), _e("projects/a/f.md")]
        text = render_listing("", entries, depth=2)
        assert text.splitlines() == [
            "workspace (all folders) — 3 files",
            "projects/ · 3 files",
            "  a/ · 3 files",
            "    b/ · 2 files · 4.0 KB",
            "    f.md · 1.0 KB · 2026-10-20 · chat",
            "(deeper folders are collapsed — view that folder to open it)",
        ]

    def test_default_depth_is_three_and_no_hint_when_nothing_collapsed(self):
        entries = [_e("projects/a/b/c.md")]
        assert "collapsed" not in render_listing("", entries)
        deep = [_e("projects/a/b/c/d.md")]
        assert "c/ · 1 file · 1.0 KB" in render_listing("", deep)
        assert "c/ · 1 file · 1.0 KB" in render_listing("", deep, depth="")
        assert "c/ · 1 file · 1.0 KB" in render_listing("", deep, depth=-2)

    def test_empty(self):
        assert render_listing("", []) == "workspace (all folders) — empty"
        assert render_listing("scratch/", []) == "workspace:scratch/ — empty"

    def test_deleted_trailer(self):
        entries = [_e("projects/a.md"), _e("projects/old.md", live=False), _e("projects/old.md", live=False)]
        assert render_listing("projects/", entries).endswith(
            "(1 deleted file in this folder — history projects/ lists them)")
        assert render_listing("", entries).endswith("(1 deleted file — history with an empty path lists them)")

    def test_entry_cap_is_a_safety_net(self):
        entries = [_e(f"projects/f{i:03d}.md") for i in range(ws.LISTING_MAX_ENTRIES + 3)]
        text = render_listing("projects/", entries)
        assert text.count(" · 1.0 KB ·") == ws.LISTING_MAX_ENTRIES
        assert text.endswith("…3 more files not shown")


class TestParsers:
    @pytest.mark.parametrize("raw,expected", [("", 3), (None, 3), (0, 3), (-1, 3), ("x", 3), ("2", 2), (5, 5)])
    def test_depth(self, raw, expected):
        assert parse_depth(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        ("", 1), (None, 1), (-1, 1), ("junk", 1), (True, 1), (0, 0), ("0", 0), (3, 3), (5, 5), (6, 5), ("99", 5),
    ])
    def test_context(self, raw, expected):
        assert parse_context(raw) == expected

    @pytest.mark.parametrize("raw,expected", [
        (True, True), (False, False), ("true", True), ("True", True), ("false", False), ("", False), (None, False),
    ])
    def test_flag(self, raw, expected):
        assert parse_flag(raw) is expected


def _grep(files, needle="deadline", scope="projects/", **kw):
    return render_grep(needle, scope, files, _contains(needle), **kw)


class TestGrep:
    def test_single_match_with_context(self):
        text, n = _grep([("projects/a.md", "one\ntwo\nthe deadline\nfour\nfive")])
        assert n == 1
        assert text.splitlines() == [
            'grep "deadline" in projects/ — 1 match in 1 file',
            "searched 1 text file: projects/ (1)",
            "projects/a.md",
            "2-two",
            "3:the deadline",
            "4-four",
        ]

    def test_adjacent_and_overlapping_windows_merge_and_groups_are_separated(self):
        body = "\n".join(
            ["deadline a", "x", "deadline b", "y", "z", "w", "v", "u", "t", "deadline c", "s"],
        )
        text, n = _grep([("projects/a.md", body)], context=1)
        assert n == 3
        assert text.splitlines()[2:] == [
            "projects/a.md",
            "1:deadline a", "2-x", "3:deadline b", "4-y",
            "--",
            "9-t", "10:deadline c", "11-s",
        ]

    def test_windows_touching_are_merged_not_separated(self):
        text, _ = _grep([("projects/a.md", "deadline\nx\ny\ndeadline")], context=1)
        assert "--" not in text and text.splitlines()[3:] == ["1:deadline", "2-x", "3-y", "4:deadline"]

    def test_context_zero_one_five_and_clamp(self):
        body = "\n".join(str(i) for i in range(1, 21)).replace("10", "deadline")
        zero, _ = _grep([("projects/a.md", body)], context=0)
        assert zero.splitlines()[3:] == ["10:deadline"]
        one, _ = _grep([("projects/a.md", body)], context=1)
        assert one.splitlines()[3:] == ["9-9", "10:deadline", "11-11"]
        five, _ = _grep([("projects/a.md", body)], context=5)
        assert len(five.splitlines()) == 3 + 11
        clamped, _ = _grep([("projects/a.md", body)], context=50)
        assert clamped == five

    def test_two_files(self):
        text, n = _grep([("projects/a.md", "deadline"), ("projects/trips/b.md", "x\ndeadline")])
        assert n == 2
        assert text.splitlines()[0] == 'grep "deadline" in projects/ — 2 matches in 2 files'
        assert text.splitlines()[3:] == ["1:deadline", "projects/trips/b.md", "1-x", "2:deadline"]

    def test_files_only(self):
        text, n = _grep([("projects/a.md", "deadline\ndeadline"), ("projects/b.md", "none")], files_only=True)
        assert n == 2
        assert text.splitlines() == [
            'grep "deadline" in projects/ — 2 matches in 1 file',
            "searched 2 text files: projects/ (2)",
            "projects/a.md · 2 matches",
        ]

    def test_no_matches_names_files_searched(self):
        text, n = _grep([("projects/a.md", "x"), ("references/b.md", "y")], needle="zzz", scope="")
        assert n == 0
        assert text == ('grep "zzz" in workspace — no matches in 2 files searched\n'
                        "searched 2 text files: projects/ (1), references/ (1)")

    def test_searched_line_single_file_and_empty_scope(self):
        text, _ = _grep([("projects/a.md", "deadline")], scope="projects/a.md")
        assert text.splitlines()[1] == "searched 1 text file: projects/a.md"
        empty, _ = _grep([], scope="scratch/")
        assert empty == 'grep "deadline" in scratch/ — no matches in 0 files searched\nsearched 0 text files'

    def test_searched_line_lists_every_top_folder_alphabetically(self):
        files = [("scratch/s.md", "q"), ("binary_files/t.txt", "q"), ("references/r.md", "q"), ("projects/p.md", "q")]
        text, _ = _grep(files, needle="q", scope="")
        assert text.splitlines()[1] == "searched 4 text files: binary_files/ (1), projects/ (1), references/ (1), scratch/ (1)"

    def test_match_cap_ends_with_trailer(self):
        body = "\n".join(["deadline"] * (GREP_MAX_MATCHES + 10))
        text, n = _grep([("projects/a.md", body)], context=0)
        assert n == GREP_MAX_MATCHES + 10
        lines = text.splitlines()
        assert lines[-1] == GREP_TRAILER
        assert sum(1 for line in lines if line.endswith(":deadline")) == GREP_MAX_MATCHES

    def test_cap_spans_files(self):
        files = [(f"projects/f{i}.md", "deadline\n" * 20) for i in range(5)]
        text, _ = _grep(files, context=0)
        assert text.splitlines()[-1] == GREP_TRAILER
        assert "projects/f2.md" in text and "projects/f3.md" not in text

    def test_byte_cap(self):
        line = "deadline " + "x" * 1500
        files = [(f"projects/f{i:02d}.md", "\n".join([line] * 3)) for i in range(20)]
        text, _ = _grep(files, context=0)
        assert text.splitlines()[-1] == GREP_TRAILER
        assert len(text.encode("utf-8")) <= GREP_MAX_OUTPUT_BYTES + len(GREP_TRAILER.encode("utf-8")) + 1
        assert "projects/f19.md" not in text

    def test_files_only_byte_cap(self):
        files = [(f"projects/{'n' * 150}{i:03d}.md", "deadline") for i in range(200)]
        text, _ = _grep(files, files_only=True)
        assert text.splitlines()[-1] == GREP_TRAILER

    def test_long_line_is_cut_like_view(self):
        text, _ = _grep([("projects/a.md", "deadline " + "x" * (VIEW_MAX_LINE_CHARS + 50))])
        shown = text.splitlines()[3]
        assert shown.endswith(" …[line cut]") and len(shown) == len("1:") + VIEW_MAX_LINE_CHARS + len(" …[line cut]")

    def test_line_numbers_match_view(self):
        text = "a\n\nb\ndeadline\n"
        number_line = ws.number_lines(text).text.splitlines()[3]
        assert number_line.split("\t")[0].strip() == "4"
        assert _grep([("projects/a.md", text)], context=0)[0].splitlines()[3] == "4:deadline"

    def test_matching_lines_uses_matcher_only(self):
        assert ws.matching_lines("a\nB\na", lambda line: line == "a") == [1, 3]
