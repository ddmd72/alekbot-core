"""The duplicate pass and the cut of the fact search: `top` keeps the first 30 after the pass, `all` every kept fact of
its set (list mode RFC §3.1: the keys define it, no cap, no kNN list); the pass compares each candidate with the facts
already kept, in that argument order."""
from tests.support.fact_search_kit import fact, query, search


def crowd(n):
    return [fact(f"f{k:03d}", domains=("money",)) for k in range(n)]


def test_top_returns_the_first_30_kept():
    out = search(crowd(45), query(domains=["money"]))
    assert len(out.fused) == 45 and out.kept == out.fused[:30]


def test_all_has_no_cut_after_the_pass():
    out = search(crowd(130), query(domains=["money"], scope="all"))
    assert len(out.lists["set"]) == 130 and out.kept == out.fused and len(out.kept) == 130


def test_all_takes_every_key_match_from_the_key_and_no_semantic_hit():
    facts = [*crowd(130), fact("stray", domains=("health",))]
    semantic = ["stray", *(f"f{k:03d}" for k in range(129, 89, -1))]
    out = search(facts, query(domains=["money"], scope="all"), semantic=semantic)
    assert list(out.lists) == ["set"] and "stray" not in out.kept
    assert sorted(out.kept) == [f"f{k:03d}" for k in range(130)]


def test_the_cut_counts_kept_facts_not_candidates():
    facts = crowd(45)
    out = search(facts, query(domains=["money"]), duplicate=lambda a, b: a in ("f001", "f002"))
    assert len(out.kept) == 30 and "f001" not in out.kept and "f002" not in out.kept
    assert out.kept == [i for i in out.fused if i not in ("f001", "f002")][:30]


def test_the_pass_asks_candidate_then_kept_in_fused_order():
    asked = []

    def dup(candidate, kept):
        asked.append((candidate, kept))
        return candidate == "f002" and kept == "f001"

    out = search(crowd(3), query(domains=["money"]), duplicate=dup)
    assert asked == [("f001", "f000"), ("f002", "f000"), ("f002", "f001")]
    assert out.kept == ["f000", "f001"] and out.fused == ["f000", "f001", "f002"]


def test_a_candidate_is_dropped_by_the_first_kept_fact_it_duplicates():
    asked = []

    def dup(candidate, kept):
        asked.append((candidate, kept))
        return True

    out = search(crowd(3), query(domains=["money"]), duplicate=dup)
    assert out.kept == ["f000"] and asked == [("f001", "f000"), ("f002", "f000")]
