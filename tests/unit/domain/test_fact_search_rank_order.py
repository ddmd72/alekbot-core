"""The scored order of one search (fact profile RFC §5.3, OD-14): `run_search` orders the candidates by score, ties by
id, after removing the excluded ones, then runs the duplicate pass and the cut. Weights are injected, so no test depends
on the fitted values."""
from src.domain.fact_search_lists import CUT, run_search
from src.domain.fact_search_rank import FEATURES, WEIGHTS, ScoreInputs, feature_values, score
from src.domain.fact_search_snapshot import FactSearchSnapshot
from tests.support.fact_search_kit import fact, query

BY_TEXT = {"sim_text": 1.0}


def run(facts, q, *, sim, exclude=frozenset(), duplicate=None, weights=None):
    return run_search(FactSearchSnapshot.from_facts(list(facts)), q, semantic_ids=None, label_ids=None,
                      similarity=sim, query_place_keys=frozenset(), is_duplicate=duplicate or (lambda a, b: False),
                      exclude=exclude, weights=BY_TEXT if weights is None else weights)


def family(n):
    return [fact(f"f{i:03d}", f"Clara Moreira fact {i}", domains=("family",)) for i in range(n)]


def test_the_fitted_weights_cover_exactly_the_features():
    assert set(WEIGHTS) == set(FEATURES)


def test_the_score_is_the_weighted_sum_of_the_features():
    f = fact("dentist", "Dentist check-up 2026-10-12", kinds=["appointment"], role="event")
    q = query(kinds=["appointment"], subject="dentist")
    inputs = ScoreInputs(sim_text={"dentist": 0.5})
    got = feature_values(q, FactSearchSnapshot.from_facts([f]).rows["dentist"], inputs)
    weights = {"kind": 2.0, "sim_text": 3.0, "role_event": -1.0}
    assert score(q, FactSearchSnapshot.from_facts([f]).rows["dentist"], inputs, weights) == \
        2.0 * got["kind"] + 3.0 * got["sim_text"] - 1.0 * got["role_event"]


def test_candidates_are_ordered_by_score():
    out = run(family(3), query(subject="Clara", domains=["family"]), sim={"f000": 0.1, "f001": 0.9, "f002": 0.5})
    assert out.fused == ["f001", "f002", "f000"] and out.kept == out.fused
    assert out.scores == {"f000": 0.1, "f001": 0.9, "f002": 0.5}


def test_a_score_tie_goes_to_the_smaller_id():
    out = run(family(3), query(subject="Clara", domains=["family"]), sim={"f000": 0.2, "f001": 0.7, "f002": 0.7})
    assert out.fused == ["f001", "f002", "f000"]
    flat = run(family(3), query(domains=["family"]), sim={})
    assert flat.fused == ["f000", "f001", "f002"]


def test_an_excluded_fact_is_never_scored_nor_kept():
    out = run(family(3), query(subject="Clara", domains=["family"]), sim={"f001": 0.9}, exclude={"f001"})
    assert "f001" not in out.fused and "f001" not in out.scores and "f001" not in out.kept
    assert "f001" in out.lists["domain"]                     # it was a candidate list member, then excluded


def test_the_duplicate_pass_runs_in_score_order():
    twins = {frozenset({"f000", "f002"})}

    def duplicate(a, b):
        return frozenset({a, b}) in twins

    first = run(family(3), query(subject="Clara", domains=["family"]), sim={"f000": 0.9, "f002": 0.1},
                duplicate=duplicate)
    assert first.fused == ["f000", "f002", "f001"] and first.kept == ["f000", "f001"]
    second = run(family(3), query(subject="Clara", domains=["family"]), sim={"f000": 0.1, "f002": 0.9},
                 duplicate=duplicate)
    assert second.kept == ["f002", "f001"]


def test_the_cut_keeps_the_first_30_after_the_duplicate_pass():
    n = CUT + 15
    sim = {f"f{i:03d}": 1.0 - i / 100 for i in range(n)}
    out = run(family(n), query(subject="Clara", domains=["family"]), sim=sim)
    assert len(out.fused) == n and out.kept == out.fused[:CUT]
    dropped = run(family(n), query(subject="Clara", domains=["family"]), sim=sim,
                  duplicate=lambda a, b: a == "f001" and b == "f000")
    assert dropped.kept == [i for i in dropped.fused if i != "f001"][:CUT]


def test_collect_all_keeps_every_candidate_after_the_duplicate_pass():
    n = CUT + 15
    out = run(family(n), query(subject="Clara", domains=["family"], scope="all"), sim={})
    assert out.kept == out.fused and len(out.kept) == n
