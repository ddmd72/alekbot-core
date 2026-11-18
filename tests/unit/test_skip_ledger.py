"""P22 skip ledger: every skip site under tests/ is in tests/skip_ledger.txt with the gate that runs it, both ways.

`UPDATE_PINS=1 pytest tests/unit/test_skip_ledger.py` (one file, no -n) rewrites the ledger from the tree; the reviewer reads its diff.
"""
import os

import pytest

from tests.support import skip_ledger as sl

LEDGER_TEXT = sl.LEDGER.read_text(encoding="utf-8") if sl.LEDGER.exists() else ""


@pytest.fixture(scope="module")
def counts():
    found = sl.scan_tree()
    if os.environ.get("UPDATE_PINS") == "1":
        sl.LEDGER.write_text(sl.render(found), encoding="utf-8")
    return found


@pytest.fixture(scope="module")
def ledger(counts):
    return sl.parse_ledger(sl.LEDGER.read_text(encoding="utf-8"))


def test_the_ledger_and_the_tree_agree_both_ways(counts, ledger):
    assert sl.diff(counts, ledger) == []


def test_every_site_is_classified(counts):
    assert [site for site in counts if sl.classify(site) is None] == []


def test_no_rule_is_stale(counts):
    assert [(r.kind, r.condition) for r in sl.unused_rules(counts)] == []


def test_every_line_has_a_known_gate_and_a_reason_of_two_words(ledger):
    for site, (_, gate, reason) in ledger.items():
        assert gate in sl.GATES, site
        assert len(reason.split()) >= 2, site


def test_every_never_line_names_what_would_run_it(ledger):
    never = sl.never_lines(ledger)
    for site in never:
        reason = ledger[site][2]
        assert "needs" in reason or "P22b" in reason or "no CI job" in reason, site


# --- the scanner and the ledger format, on invented sources ----------------------------------------------------------

SOURCE = '''
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_FFMPEG, reason="x")]

needs_node = pytest.mark.skipif(not HAVE_NODE, reason="n")

@pytest.mark.skipif(not HAVE_FFMPEG, reason="y")
@pytest.mark.timing
def test_a():
    pytest.skip("Firestore only")

@needs_node
def test_b():
    pytest.importorskip("lxml")

@needs_node
class TestGroup:
    def test_one(self): pass
    def test_two(self): pass
'''


def test_the_scanner_finds_each_kind_of_site_and_counts_the_tests_behind_it():
    sites = sl.scan_source("tests/x.py", SOURCE)
    # pytestmark: the module's 4 tests; the decorator on test_a: 1; needs_node: test_b (1) + the class (2 tests)
    assert sorted(sites) == sorted([("tests/x.py", "marker", "slow"), ("tests/x.py", "marker", "timing")]
                                   + [("tests/x.py", "skipif", "not HAVE_FFMPEG")] * 5
                                   + [("tests/x.py", "skipif", "not HAVE_NODE")] * 3
                                   + [("tests/x.py", "skip", "'Firestore only'"), ("tests/x.py", "importorskip", "'lxml'")])


def test_a_class_level_pytestmark_counts_the_tests_of_the_class():
    source = "import pytest\nclass TestA:\n    pytestmark = pytest.mark.skipif(X, reason='r')\n    def test_a(self): pass\n    def test_b(self): pass\ndef test_c(): pass\n"
    assert sl.scan_source("tests/x.py", source) == [("tests/x.py", "skipif", "X")] * 2


@pytest.mark.parametrize("source,expected", [
    ("import pytest\n@pytest.mark.skip\ndef test_a(): pass\n", [("mark.skip", "<bare>")]),
    ("import pytest\n@pytest.mark.skip(reason='r')\ndef test_a(): pass\n", [("mark.skip", "reason='r'")]),
    ("from pytest import skip\ndef test_a():\n    skip('why')\n", [("skip", "'why'")]),
    ("import pytest as pt\n@pt.mark.skipif(C, reason='r')\ndef test_a(): pass\n", [("skipif", "C")]),
    ("import unittest\n@unittest.skip('r')\ndef test_a(): pass\n", [("mark.skip", "'r'")]),
    ("import unittest\n@unittest.skipIf(C, 'r')\ndef test_a(): pass\n", [("skipif", "C")]),
    ("import unittest\ndef test_a():\n    raise unittest.SkipTest('r')\n", [("skip", "'r'")]),
    ("from pytest import mark\n@mark.slow\ndef test_a(): pass\n", [("marker", "slow")]),
    ("from pytest import mark as m\n@m.skipif(C, reason='r')\ndef test_a(): pass\n", [("skipif", "C")]),
    ("def skip(x): pass\ndef test_a():\n    skip('not pytest')\n", []),
])
def test_the_scanner_resolves_imports_and_bare_forms(source, expected):
    assert [(kind, condition) for _, kind, condition in sl.scan_source("tests/x.py", source)] == expected


def test_a_string_that_mentions_a_skip_is_not_a_site():
    assert sl.scan_source("tests/x.py", 'X = "pytest.mark.skipif(True)"\n') == []


def test_a_ledger_round_trips_and_a_drift_is_named():
    counts = {("tests/x.py", "skipif", "not HAVE_FFMPEG"): 2, ("tests/x.py", "marker", "slow"): 1}
    text = sl.render(counts)
    parsed = sl.parse_ledger(text)
    assert sl.diff(counts, parsed) == []
    counts[("tests/x.py", "skipif", "not HAVE_FFMPEG")] = 3
    counts[("tests/y.py", "marker", "timing")] = 1
    del counts[("tests/x.py", "marker", "slow")]
    problems = "\n".join(sl.diff(counts, parsed))
    assert "count differs (2 in the ledger, 3 in the tree)" in problems
    assert "not in the ledger: tests/y.py" in problems
    assert "skip site is gone: tests/x.py :: marker :: slow" in problems


def test_an_unclassified_site_is_reported_not_hidden():
    site = ("tests/x.py", "skipif", "some_new_condition")
    assert sl.classify(site) is None
    assert "unclassified" in sl.render({site: 1})
    assert "no rule classifies" in "\n".join(sl.diff({site: 1}, {site: (1, "ci", "a reason")}))


def test_a_malformed_ledger_line_is_refused():
    with pytest.raises(ValueError):
        sl.parse_ledger("tests/x.py :: skipif :: c :: x1\n")
    with pytest.raises(ValueError):
        sl.parse_ledger("tests/x.py :: skipif :: c  # ci: why\n")
