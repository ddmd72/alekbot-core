"""Mutation echo (AGENT_WORKSPACE_RFC §7.3, D18): the changed block with one context line each side."""
from src.domain.workspace import (
    VIEW_MAX_LINE_CHARS,
    apply_insert,
    apply_str_replace,
    render_mutation_echo,
)

P = "projects/garden-shed.md"
FILE = "survey booked\nplans drawn\nform submitted\nfee paid\ndecision expected\n"


def _n(n, line):
    return f"{n:6d}\t{line}"


class TestInsert:
    def test_middle_insert_has_context_both_sides(self):
        r = apply_insert(P, FILE, 2, "neighbour letter sent")
        assert render_mutation_echo(r) == "\n".join(
            [_n(2, "plans drawn"), _n(3, "neighbour letter sent"), _n(4, "form submitted")])

    def test_insert_at_top_has_no_before_context(self):
        r = apply_insert(P, FILE, 0, "idea: shed")
        assert render_mutation_echo(r) == "\n".join([_n(1, "idea: shed"), _n(2, "survey booked")])

    def test_insert_at_end_has_no_after_context(self):
        r = apply_insert(P, FILE, -1, "permission granted")
        assert render_mutation_echo(r) == "\n".join([_n(5, "decision expected"), _n(6, "permission granted")])

    def test_one_line_file(self):
        r = apply_insert(P, "only\n", -1, "next")
        assert render_mutation_echo(r) == "\n".join([_n(1, "only"), _n(2, "next")])
        r = apply_insert(P, "", 0, "first")
        assert render_mutation_echo(r) == _n(1, "first")

    def test_ten_lines_are_shown_in_full(self):
        text = "\n".join(f"n{i}" for i in range(1, 11)) + "\n"
        r = apply_insert(P, "head\ntail\n", 1, text)
        out = render_mutation_echo(r).splitlines()
        assert out == [_n(1, "head")] + [_n(1 + i, f"n{i}") for i in range(1, 11)] + [_n(12, "tail")]

    def test_eleven_lines_show_their_edges(self):
        text = "\n".join(f"n{i}" for i in range(1, 12)) + "\n"
        r = apply_insert(P, "head\ntail\n", 1, text)
        assert render_mutation_echo(r).splitlines() == [
            _n(1, "head"), _n(2, "n1"), _n(3, "n2"), "   … 7 more lines …", _n(11, "n10"), _n(12, "n11"),
            _n(13, "tail"),
        ]


class TestStrReplace:
    def test_single_line(self):
        r = apply_str_replace(P, FILE, "fee paid", "fee paid (receipt No. 0412)")
        assert render_mutation_echo(r) == "\n".join(
            [_n(3, "form submitted"), _n(4, "fee paid (receipt No. 0412)"), _n(5, "decision expected")])

    def test_multi_line(self):
        r = apply_str_replace(P, FILE, "plans drawn\nform submitted", "plans revised\nform resubmitted\nfee queried")
        assert render_mutation_echo(r) == "\n".join([
            _n(1, "survey booked"), _n(2, "plans revised"), _n(3, "form resubmitted"), _n(4, "fee queried"),
            _n(5, "fee paid")])

    def test_at_first_line_has_no_before_context(self):
        r = apply_str_replace(P, FILE, "survey booked", "survey done")
        assert render_mutation_echo(r).splitlines() == [_n(1, "survey done"), _n(2, "plans drawn")]

    def test_empty_replacement_shows_only_the_lines_around_the_cut(self):
        r = apply_str_replace(P, FILE, "form submitted\n", "")
        assert r.line_count == 0
        assert render_mutation_echo(r) == "\n".join([_n(2, "plans drawn"), _n(3, "fee paid")])

    def test_empty_replacement_at_the_end(self):
        r = apply_str_replace(P, "a\nb\nc\n", "c\n", "")
        assert render_mutation_echo(r) == _n(2, "b")

    def test_empty_replacement_inside_a_line_shows_the_joined_line(self):
        r = apply_str_replace(P, "a\nold name here\nc\n", "old ", "")
        assert render_mutation_echo(r) == "\n".join([_n(1, "a"), _n(2, "name here")])


def test_a_long_line_is_cut_like_a_view():
    r = apply_insert(P, "a\nb\n", 1, "x" * (VIEW_MAX_LINE_CHARS + 50))
    assert render_mutation_echo(r).splitlines()[1] == _n(2, "x" * VIEW_MAX_LINE_CHARS + " …[line cut]")
