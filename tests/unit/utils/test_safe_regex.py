"""Line matchers for workspace grep: literal mode without a regex engine, regex mode on RE2."""
import pytest

from src.utils.safe_regex import InvalidPattern, compile_line_matcher


def test_literal_is_not_a_regex():
    m = compile_line_matcher("a.b(c)*[x]")
    assert m("see a.b(c)*[x] here")
    assert not m("aXb(c)")


def test_literal_case_sensitive_by_default_and_ignore_case():
    assert not compile_line_matcher("Deadline")("the deadline")
    assert compile_line_matcher("Deadline", ignore_case=True)("THE DEADLINE")


def test_ignore_case_casefolds_beyond_ascii():
    assert compile_line_matcher("straße", ignore_case=True)("STRASSE")


def test_regex_mode():
    m = compile_line_matcher(r"dead.*\d+", regex=True)
    assert m("deadline 42")
    assert not m("deadline")
    assert compile_line_matcher("DEAD", regex=True, ignore_case=True)("deadline")
    assert not compile_line_matcher("DEAD", regex=True)("deadline")


def test_metacharacters_are_literal_without_regex_flag_but_not_with_it():
    assert compile_line_matcher("a+")("a+")
    assert not compile_line_matcher("a+")("aaa")
    assert compile_line_matcher("a+", regex=True)("aaa")


@pytest.mark.parametrize("pattern", ["(a)\\1", "(?=a)b", "(?<!a)b", "(unclosed"])
def test_re2_rejects_unsupported_or_invalid_patterns(pattern):
    with pytest.raises(InvalidPattern) as e:
        compile_line_matcher(pattern, regex=True)
    assert "RE2 syntax: no backreferences or lookaround" in str(e.value)


def test_backreference_is_fine_as_literal():
    assert compile_line_matcher("(a)\\1")("x (a)\\1 y")


def test_catastrophic_pattern_finishes():
    assert not compile_line_matcher("(a+)+$", regex=True)("a" * 5000 + "b")
