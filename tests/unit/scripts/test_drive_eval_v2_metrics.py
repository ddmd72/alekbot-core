"""Search v2 eval metrics (USER_DRIVE_INDEXING_RFC §25.7; v2_metrics.py): each family's pass rule, junk, the book
distractor, the family table, the pre-registered bars' verdict, the v1 mapping (every v1 card is a claimed match) and
the v1 gold under v2."""
import pytest

from scripts.validation.drive_eval.v2_metrics import (
    BOOK, GoldError, V1_TIER, case_pass, family_table, g1_family, g1_table, judge, junk, validate_gold_v2,
)

A, B, C = "Docs/a.pdf", "Docs/b.pdf", "Docs/c.pdf"


def case(family, expected=(), **kw):
    return {"id": kw.pop("id", f"{family}-1"), "family": family, "query": kw.pop("query", "words here"),
            "expected_paths": list(expected), "expect_tier": kw.pop("expect_tier", "any"), **kw}


def row(tiers, **kw):
    return {"tiers": dict(tiers), "concepts": kw.pop("concepts", ["words", "here"]), **kw}


@pytest.mark.parametrize("tier, exact, semantic, klass", [
    ("about", True, True, True), ("mentioned", True, True, True), ("translated", False, True, True),
    ("labelled", False, True, True), ("related", False, True, False), (None, False, False, False),
])
def test_pass_rules_by_family(tier, exact, semantic, klass):
    r = row({A: tier} if tier else {})
    assert case_pass(case("exact", [A]), r) is exact
    assert case_pass(case("semantic", [A]), r) is semantic
    assert case_pass(case("class", [A]), r) is klass


def test_every_expected_file_is_needed():
    assert not case_pass(case("exact", [A, B]), row({A: "about"}))


def test_a_negative_passes_with_related_lines_only():
    assert case_pass(case("negative"), row({A: "related"}))
    assert not case_pass(case("negative"), row({A: "labelled"}))


def test_a_misspelling_passes_by_the_d29_line_or_by_showing_the_file():
    c = case("misspelling", [A], expect_absent=["garantai"])
    assert case_pass(c, row({}, absent=["garantai"]))
    assert case_pass(c, row({A: "related"}))
    assert not case_pass(c, row({B: "about"}))
    assert case_pass(case("misspelling", expect_absent=["barcleona"]), row({}, absent=["barcleona"]))


def test_junk_excludes_expected_and_acceptable_files():
    c = case("exact", [A], acceptable_paths=[B])
    assert junk(c, row({A: "about", B: "mentioned", C: "mentioned", "D": "labelled"}), {"about", "mentioned"}) == 1


def test_the_book_counts_only_on_multi_word_cases_not_about_it():
    t = family_table([case("exact", [A], id="x")], {"x": row({A: "about", BOOK: "mentioned"})})
    assert t["book_in_exact"] == ["x"]
    t = family_table([case("exact", [A], id="x")], {"x": row({A: "about", BOOK: "mentioned"}, concepts=["rome"])})
    assert t["book_in_exact"] == []
    t = family_table([case("exact", [A], id="x", acceptable_paths=[BOOK])], {"x": row({A: "about", BOOK: "about"})})
    assert t["book_in_exact"] == []


def test_family_table_and_judge():
    gold = [case("exact", [A], id="e1"), case("exact", [B], id="e2"), case("negative", id="n1"),
            case("class", [A, B], id="c1")]
    rows = {"e1": row({A: "about"}, chars=1000), "e2": row({}, chars=2000), "n1": row({C: "related"}, chars=500),
            "c1": row({A: "labelled", C: "labelled"}, chars=4000)}
    t = family_table(gold, rows)
    assert t["families"]["exact"]["rate"] == 0.5 and t["families"]["class"]["file_recall"] == 0.5
    assert t["related_per_negative"] == 1 and t["automatic_junk_per_case"] == pytest.approx(1 / 3)
    assert t["chars_median"] == 1500 and t["chars_max"] == 4000
    bars = {"pass_rate": {"exact": 0.95, "negative": 0.9}, "file_recall": {"class": 0.85},
            "exact_junk_per_case_max": 0.3, "automatic_junk_per_case_max": 0.5, "related_per_negative_max": 1.0,
            "book_in_exact_max": 0, "chars_median_max": 3000, "chars_max": 16000}
    failures = judge(t, {"lost": []}, bars)
    assert any("exact pass rate" in f for f in failures) and any("class file recall" in f for f in failures)
    assert not any("negative" in f for f in failures)


def test_v1_cards_are_scored_as_claimed_matches():
    gold = [case("exact", [A], id="e"), case("negative", id="n")]
    rows = {"e": {"tiers": {A: V1_TIER, B: V1_TIER}}, "n": {"tiers": {C: V1_TIER}}}
    t = family_table(gold, rows, v1=True)
    assert t["families"]["exact"]["passed"] == 1 and t["families"]["negative"]["passed"] == 0
    assert t["exact_junk_per_case"] == 1


def test_v1_gold_under_v2_keeps_every_v1_passing_case():
    gold = [{"id": "g1", "case": "exact", "expected_paths": [A]}, {"id": "g2", "case": "translit",
                                                                  "expected_paths": [B]}]
    rows = {"g1": row({A: "labelled"}), "g2": row({B: "translated"})}
    t = g1_table(gold, rows, {"g1": True, "g2": True})
    assert t["lost"] == ["g1"] and t["passed"] == 1
    assert g1_family({"case": "misspelled"}) == "misspelling" and g1_family({"case": "clause"}) == "class"


def test_a_listing_is_scored_by_v1s_top_k_rule():
    gold = [{"id": "f", "case": "filter_only", "expected_paths": [A]}]
    assert g1_table(gold, {"f": {"listing": True, "tiers": {A: "listing"}}}, {"f": True})["lost"] == []


def test_validate_refuses_malformed_cases():
    with pytest.raises(GoldError) as err:
        validate_gold_v2([case("exact", id="a"), case("negative", [A], id="b"), case("bogus", id="c"),
                          case("exact", ["Nope/x.pdf"], id="d"), case("exact", [A], id="d", query="")], [A])
    text = str(err.value)
    for part in ("a: needs expected_paths (family exact)", "b: a negative case", "unknown family 'bogus'", "Nope/x.pdf",
                 "duplicate id d", "needs words"):
        assert part in text
