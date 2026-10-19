"""Drive eval harness fixes (USER_DRIVE_INDEXING_RFC §26.10 Δ94): the G2 window is the v2 card's `passage`;
`requires_embeddings` cases are skipped under lexical vectors; `accepted_loss` cases leave the "lost" bar."""
from types import SimpleNamespace

import pytest

from scripts.validation.drive_eval.metrics import substring_hit
from scripts.validation.drive_eval.runner import EvalError, _windows, validate_gold
from scripts.validation.drive_eval.v2_metrics import (
    GoldError, family_table, g1_table, judge, split_by_embeddings, validate_gold_v2,
)

A = "Docs/a.pdf"


def test_windows_read_the_cards_passage():
    drive = SimpleNamespace(path_of=lambda _i: A)
    card = SimpleNamespace(ref="", path=A, also_at=(), passage="deposit EUR 2,500.00")   # no `window` attribute
    windows = _windows([card], drive)
    assert windows == {A: "deposit EUR 2,500.00"}
    assert substring_hit({A: "EUR 2,500.00"}, windows)


def v2case(cid, **kw):
    return {"id": cid, "family": "semantic", "query": "words", "expected_paths": [A], "expect_tier": "related", **kw}


def test_semantic_only_cases_are_skipped_under_lexical_and_counted_under_gemini():
    gold = [v2case("a"), v2case("b", requires_embeddings="semantic")]
    kept, skipped = split_by_embeddings(gold, "lexical")
    assert [c["id"] for c in kept] == ["a"] and [c["id"] for c in skipped] == ["b"]
    kept, skipped = split_by_embeddings(gold, "gemini")
    assert [c["id"] for c in kept] == ["a", "b"] and skipped == []
    rows = {"a": {"tiers": {A: "related"}}}   # no row for the skipped case: the table must not need one
    assert family_table(kept[:1], rows)["families"]["semantic"]["cases"] == 1


def test_requires_embeddings_value_is_validated():
    with pytest.raises(GoldError):
        validate_gold_v2([v2case("a", requires_embeddings="vector")], [A])
    validate_gold_v2([v2case("a", requires_embeddings="semantic")], [A])


def g1case(cid, **kw):
    return {"id": cid, "case": "exact", "query": "word", "expected_paths": [A], **kw}


def test_an_accepted_loss_is_listed_and_not_a_lost_case():
    gold = [g1case("x", accepted_loss="D29"), g1case("y")]
    rows = {"x": {"tiers": {}}, "y": {"tiers": {}}}
    table = g1_table(gold, rows, {"x": True, "y": True})
    assert table["lost"] == ["y"] and table["accepted_losses"] == {"x": "D29"}
    only_accepted = g1_table(gold[:1], rows, {"x": True})
    assert only_accepted["lost"] == []
    bars = {"pass_rate": {}, "file_recall": {}, "exact_junk_per_case_max": 9, "automatic_junk_per_case_max": 9,
            "related_per_negative_max": 9, "book_in_exact_max": 9, "chars_median_max": 9, "chars_max": 9}
    assert judge({"families": {}}, only_accepted, bars) == []


def test_accepted_loss_needs_a_reason_in_the_v1_gold():
    with pytest.raises(EvalError):
        validate_gold([g1case("x", accepted_loss=" ")], [A])
    validate_gold([g1case("x", accepted_loss="D29")], [A])
