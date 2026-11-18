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


def test_a_file_without_a_skip_has_no_line(ledger):
    assert all(path.startswith("tests/") for path, _, _ in ledger)


# --- the scanner and the ledger format, on invented sources ----------------------------------------------------------

SOURCE = '''
import pytest
pytestmark = [pytest.mark.slow, pytest.mark.skipif(not HAVE_FFMPEG, reason="x")]

@pytest.mark.skipif(not HAVE_FFMPEG, reason="y")
@pytest.mark.timing
def test_a():
    pytest.skip("Firestore only")

def test_b():
    pytest.importorskip("lxml")
    pytest.mark.skip(reason="r")
'''


def test_the_scanner_finds_each_kind_of_site():
    sites = sl.scan_source("tests/x.py", SOURCE)
    assert sorted(sites) == sorted([
        ("tests/x.py", "marker", "slow"), ("tests/x.py", "marker", "timing"),
        ("tests/x.py", "skipif", "not HAVE_FFMPEG"), ("tests/x.py", "skipif", "not HAVE_FFMPEG"),
        ("tests/x.py", "skip", "'Firestore only'"), ("tests/x.py", "importorskip", "'lxml'"),
        ("tests/x.py", "mark.skip", "reason='r'"),
    ])


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
