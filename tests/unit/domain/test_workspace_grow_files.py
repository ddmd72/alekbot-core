"""Growing files (AGENT_WORKSPACE_RFC D18): apply_insert, negative offsets, the long-file outline, multi-match line
lists and the 3 MB archive tail. Pure domain."""
import pytest

from src.domain.exceptions import WorkspaceRefused
from src.domain.workspace import (
    MAX_TEXT_FILE_BYTES,
    MULTI_MATCH_LINES,
    OUTLINE_MAX_HEADINGS,
    VIEW_FULL_MAX_CHARS,
    apply_insert,
    apply_str_replace,
    check_file_size,
    is_long_text,
    is_whole_file_request,
    number_lines,
    parse_insert_line,
    render_outline,
    view_window,
    window_note,
)

P = "projects/garden-shed.md"


class TestApplyInsert:
    def test_append_to_a_file_ending_with_a_newline(self):
        r = apply_insert(P, "a\nb\n", -1, "c")
        assert (r.text, r.first_line, r.line_count) == ("a\nb\nc\n", 3, 1)

    def test_append_to_a_file_without_a_trailing_newline(self):
        r = apply_insert(P, "a\nb", -1, "c")
        assert (r.text, r.first_line, r.line_count) == ("a\nb\nc", 3, 1)

    def test_append_to_an_empty_file(self):
        for line in (0, -1):
            r = apply_insert(P, "", line, "x\ny")
            assert (r.text, r.first_line, r.line_count) == ("x\ny", 1, 2)

    def test_insert_at_the_top(self):
        r = apply_insert(P, "a\nb\n", 0, "z")
        assert (r.text, r.first_line, r.line_count) == ("z\na\nb\n", 1, 1)

    def test_insert_in_the_middle(self):
        r = apply_insert(P, "a\nb\nc\n", 1, "m1\nm2")
        assert (r.text, r.first_line, r.line_count) == ("a\nm1\nm2\nb\nc\n", 2, 2)

    def test_after_the_last_line_by_number_equals_minus_one(self):
        assert apply_insert(P, "a\nb\n", 2, "c").text == apply_insert(P, "a\nb\n", -1, "c").text

    def test_minus_one_on_a_one_line_file(self):
        r = apply_insert(P, "a", -1, "b")
        assert (r.text, r.first_line) == ("a\nb", 2)

    @pytest.mark.parametrize("file_text", ["a\nb\n", "a\nb"])
    @pytest.mark.parametrize("line", [0, 1, 2, -1])
    def test_a_trailing_newline_on_the_inserted_text_changes_nothing(self, file_text, line):
        assert apply_insert(P, file_text, line, "x") == apply_insert(P, file_text, line, "x\n")

    def test_a_blank_line_is_inserted_only_when_asked_for(self):
        assert apply_insert(P, "a\n", -1, "\n").text == "a\n\n"
        assert apply_insert(P, "a\n", -1, "b\n\n").line_count == 2

    def test_other_line_endings_are_not_rewritten(self):
        assert apply_insert(P, "a\r\nb\r\n", 1, "q").text == "a\r\nq\nb\r\n"

    @pytest.mark.parametrize("line", [-2, 3, 99])
    def test_out_of_range(self, line):
        with pytest.raises(WorkspaceRefused) as e:
            apply_insert(P, "a\nb\n", line, "x")
        assert str(e.value) == (
            f"insert_line {line} is out of range: the file has 2 lines; use 0..2, or -1 for the end")

    def test_empty_text_is_refused(self):
        with pytest.raises(WorkspaceRefused) as e:
            apply_insert(P, "a\n", -1, "")
        assert e.value.reason == "empty"

    def test_parse_insert_line(self):
        assert [parse_insert_line(x) for x in (0, "0", -1, " -1 ", 7)] == [0, 0, -1, -1, 7]
        for bad in (None, "", "end", True, "1.5"):
            with pytest.raises(WorkspaceRefused):
                parse_insert_line(bad)


def _lines(n):
    return "\n".join(f"line {i}" for i in range(1, n + 1))


class TestNegativeOffset:
    def test_last_lines(self):
        w = number_lines(_lines(100), -50)
        assert (w.first, w.last, w.total, w.cut) == (51, 100, 100, False)
        assert w.text.splitlines()[0] == "    51\tline 51"
        assert window_note(w) == ""

    def test_limit_still_applies(self):
        w = number_lines(_lines(100), -50, 10)
        assert (w.first, w.last) == (51, 60)
        assert window_note(w) == "…lines 51–60 of 100; view with offset=61 to read on"

    def test_longer_than_the_file_starts_at_one(self):
        assert view_window(-500, None, 100) == (1, 2000)

    def test_string_offsets(self):
        assert view_window("-3", "", 10) == (8, 2000)
        assert view_window("", "", 10) == (1, 2000)
        assert view_window("0", 0, 10) == (1, 2000)
        assert view_window("junk", -5, 10) == (1, 2000)
        assert view_window(4, 3, 10) == (4, 3)

    def test_limit_keeps_zero_or_negative_as_default(self):
        assert view_window(1, -7, 10)[1] == 2000

    def test_empty_file(self):
        assert number_lines("", -50).total == 0


class TestOutline:
    def test_whole_file_request(self):
        assert is_whole_file_request(None, None) and is_whole_file_request("", 0) and is_whole_file_request("junk", -1)
        assert not is_whole_file_request(1, None) and not is_whole_file_request(None, 10)
        assert not is_whole_file_request(-50, None)

    def test_threshold_is_in_characters(self):
        assert not is_long_text("x" * VIEW_FULL_MAX_CHARS)
        assert is_long_text("x" * (VIEW_FULL_MAX_CHARS + 1))

    def test_headings_and_hint(self):
        text = "# Garden shed\nintro\n## Decision\nbody\n#nospace\n####### seven\n### Neighbours\n"
        out = render_outline(text, 2048).splitlines()
        assert out[:3] == ["     1\t# Garden shed", "     3\t## Decision", "     7\t### Neighbours"]
        assert out[3] == (
            "(long file: 7 lines, 2.0 KB — text not shown. Read a part with offset and limit; a negative offset "
            "reads from the end, -50 = the last 50 lines. Find lines with grep.)")
        assert len(out) == 4

    def test_no_headings_shows_only_the_hint(self):
        out = render_outline("plain\ntext\n", 11)
        assert out.startswith("(long file: 2 lines, 11 B — text not shown.") and "\n" not in out

    def test_more_than_the_cap_of_headings(self):
        n = OUTLINE_MAX_HEADINGS + 5
        out = render_outline("\n".join(f"## H{i}" for i in range(n)), 10).splitlines()
        assert out[OUTLINE_MAX_HEADINGS] == "…5 more headings"
        assert len(out) == OUTLINE_MAX_HEADINGS + 2


class TestMultiMatchAndSizes:
    def test_lines_of_the_matches(self):
        text = "x\nfoo\nbar\nfoo\nfoo\n"
        with pytest.raises(WorkspaceRefused) as e:
            apply_str_replace(P, text, "foo", "baz")
        assert str(e.value) == f"found 3 matches of old_str in {P} at lines 2, 4, 5; include more surrounding text"

    def test_only_the_first_twenty_lines_are_listed(self):
        text = "foo\n" * (MULTI_MATCH_LINES + 3)
        with pytest.raises(WorkspaceRefused) as e:
            apply_str_replace(P, text, "foo", "baz")
        listed = ", ".join(str(i) for i in range(1, MULTI_MATCH_LINES + 1))
        assert f"found 23 matches of old_str in {P} at lines {listed}, …; include" in str(e.value)

    def test_exactly_twenty_has_no_ellipsis(self):
        with pytest.raises(WorkspaceRefused) as e:
            apply_str_replace(P, "foo\n" * MULTI_MATCH_LINES, "foo", "baz")
        assert "…" not in str(e.value)

    def test_3mb_refusal_names_the_fix(self):
        with pytest.raises(WorkspaceRefused) as e:
            check_file_size("projects/garden-shed-log.md", b"a" * (MAX_TEXT_FILE_BYTES + 1))
        assert str(e.value) == (
            "projects/garden-shed-log.md: text file over 3 MB — rename it to an archive name "
            "(e.g. projects/garden-shed-log-archive-1.md) and start a new file")
