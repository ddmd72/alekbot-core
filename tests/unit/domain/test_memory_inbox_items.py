"""The memory inbox tool's liberal input (MCP_MEMORY_SUBMISSIONS_RFC §5.2): every plausible shape of `items` becomes
drafts, never an error; empties are dropped and counted, items past MAX_ITEMS_PER_CALL are dropped and counted, long
fields are cut at their cap and named, an unknown basis is `inferred`."""
import pytest

from src.domain.memory_inbox import MAX_INFORMATION_CHARS, MAX_ITEMS_PER_CALL, MAX_WHEN_CHARS, Basis
from src.domain.memory_inbox_items import normalize_items

CLARA = "Clara booked the Coimbra weekend"

ROWS = [  # raw -> (drafts as (information, basis, said_when, happens_when), dropped_empty, over_call)
    (CLARA, ([(CLARA, "inferred", "", "")], 0, 0)),
    (f"  {CLARA}  ", ([(CLARA, "inferred", "", "")], 0, 0)),
    (["a", "b"], ([("a", "inferred", "", ""), ("b", "inferred", "", "")], 0, 0)),
    ('[{"information": "a", "basis": "said"}]', ([("a", "said", "", "")], 0, 0)),
    ('{"information": "a", "happens_when": "March 6-8"}', ([("a", "inferred", "", "March 6-8")], 0, 0)),
    ('["a", "b"]', ([("a", "inferred", "", ""), ("b", "inferred", "", "")], 0, 0)),
    ("[not json", ([("[not json", "inferred", "", "")], 0, 0)),
    ({"information": "a", "basis": " DOCUMENT "}, ([("a", "document", "", "")], 0, 0)),
    ([{"information": "a", "basis": "Said"}], ([("a", "said", "", "")], 0, 0)),
    ([{"information": "a", "basis": "the user told me"}], ([("a", "inferred", "", "")], 0, 0)),
    ([{"information": "a", "basis": 3}], ([("a", "inferred", "", "")], 0, 0)),
    ([{"information": "  "}, {"information": None}], ([], 2, 0)),
    ([None, "", {"basis": "said"}, "a"], ([("a", "inferred", "", "")], 3, 0)),
    ([{"information": "a", "said_when": None, "happens_when": None}], ([("a", "inferred", "", "")], 0, 0)),
    ([{"information": "a", "said_when": " last week ", "happens_when": " early March "}],
     ([("a", "inferred", "last week", "early March")], 0, 0)),
    ([{"information": 20}], ([("20", "inferred", "", "")], 0, 0)),
    ([{"information": "a", "unknown_field": "x"}], ([("a", "inferred", "", "")], 0, 0)),
    (None, ([], 0, 0)),
    ([], ([], 0, 0)),
    ("", ([], 1, 0)),
    ([["a"], {"information": ["a"]}], ([], 2, 0)),
    ([f"item {i}" for i in range(MAX_ITEMS_PER_CALL + 1)],
     ([(f"item {i}", "inferred", "", "") for i in range(MAX_ITEMS_PER_CALL)], 0, 1)),
    (["", *(f"item {i}" for i in range(MAX_ITEMS_PER_CALL))],          # an empty is not counted against the cap
     ([(f"item {i}", "inferred", "", "") for i in range(MAX_ITEMS_PER_CALL)], 1, 0)),
]


@pytest.mark.parametrize("raw, expected", ROWS)
def test_every_shape_becomes_drafts(raw, expected):
    drafts, dropped_empty, over_call = expected
    n = normalize_items(raw)
    assert [(d.information, d.basis.value, d.said_when, d.happens_when) for d in n.drafts] == drafts
    assert (n.dropped_empty, n.over_call, n.cut_fields) == (dropped_empty, over_call, 0)
    assert all(d.cut == frozenset() for d in n.drafts)


def test_long_fields_are_cut_and_named():
    n = normalize_items([{"information": "x" * (MAX_INFORMATION_CHARS + 1), "said_when": "y" * (MAX_WHEN_CHARS + 1),
                          "happens_when": "z" * MAX_WHEN_CHARS}, "short"])
    first, second = n.drafts
    assert len(first.information) == MAX_INFORMATION_CHARS and len(first.said_when) == MAX_WHEN_CHARS
    assert len(first.happens_when) == MAX_WHEN_CHARS
    assert first.cut == {"information", "said_when"} and second.cut == frozenset() and n.cut_fields == 2


def test_the_basis_is_typed():
    assert normalize_items({"information": "a", "basis": "document"}).drafts[0].basis is Basis.DOCUMENT
