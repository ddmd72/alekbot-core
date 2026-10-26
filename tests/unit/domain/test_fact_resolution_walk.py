"""`ForwardWalk` (T12c D-M5): the forward walk of `resolve_current` as a sans-I/O stepper. A store reads what `wanted` names
and feeds it back; the same cases as `test_fact_resolution.py` run through the loop every store writes."""
from typing import Dict, List, Optional

import pytest

from src.domain.entities import FactEntity, FactState
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import CABINET_ACCEPTS, CONSOLIDATION_ACCEPTS, MAX_FORWARD_HOPS
from src.domain.fact_resolution import ForwardWalk
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, OTHER, T0, make_fact


def _closed(name: str, successor: str, **over) -> FactEntity:
    return make_fact(name, state=FactState.SUPERSEDED, is_current=False, valid_to=T0, superseded_by=successor, **over)


def _store(facts: List[FactEntity]) -> Dict[str, StoredFact]:
    return {f.id: StoredFact(f, f"token-{f.id}") for f in facts}


def _run(stored: Dict[str, StoredFact], start: str, accepted) -> tuple[Optional[StoredFact], List[str]]:
    """What a store does: read what the walk wants until it wants nothing. Returns the result and the ids read."""
    walk = ForwardWalk(start, ACC, accepted)
    read: List[str] = []
    while walk.wanted is not None:
        read.append(walk.wanted)
        walk.feed(stored.get(walk.wanted))
    return walk.result, read


def test_the_start_is_the_first_document_wanted():
    assert ForwardWalk("f-x", ACC, CABINET_ACCEPTS).wanted == "f-x"


def test_a_document_in_the_accepted_states_ends_the_walk_with_itself():
    got, read = _run(_store([make_fact("x")]), "f-x", CABINET_ACCEPTS)

    assert got is not None and got.fact.id == "f-x" and got.update_time == "token-f-x"
    assert read == ["f-x"]


def test_a_superseded_chain_is_followed_one_read_per_document_to_the_accepted_one():
    stored = _store([
        _closed("v1", "f-v2", lineage_id="L"), _closed("v2", "f-v3", lineage_id="L", version=2),
        make_fact("v3", lineage_id="L", version=3, state=FactState.ARCHIVED),
    ])

    got, read = _run(stored, "f-v1", CABINET_ACCEPTS)

    assert got.fact.id == "f-v3" and read == ["f-v1", "f-v2", "f-v3"]


@pytest.mark.parametrize("start", ["f-missing", "f-foreign"])
def test_a_missing_start_or_one_of_another_account_ends_with_no_result(start):
    got, read = _run(_store([make_fact("foreign", account=OTHER)]), start, CABINET_ACCEPTS)

    assert got is None and read == [start]


def test_a_foreign_start_is_never_given_to_forward_step(monkeypatch):
    import src.domain.fact_resolution as module

    monkeypatch.setattr(module, "forward_step", lambda *a, **k: pytest.fail("forward_step saw a foreign document"))

    assert _run(_store([_closed("x", "f-y", account=OTHER)]), "f-x", CABINET_ACCEPTS)[0] is None


def test_a_missing_successor_is_a_refusal():
    with pytest.raises(FactResolutionError) as caught:
        _run(_store([_closed("lost", "f-gone", lineage_id="LL")]), "f-lost", CABINET_ACCEPTS)

    assert str(caught.value) == "fact f-lost was superseded and its successor is unknown; search again"


def test_a_successor_in_another_lineage_is_a_merge_refusal_naming_it():
    stored = _store([_closed("a", "f-m", lineage_id="LA"), make_fact("m", lineage_id="LM")])

    with pytest.raises(FactResolutionError) as caught:
        _run(stored, "f-a", CABINET_ACCEPTS)

    assert str(caught.value) == "fact f-a was merged into f-m; search again" and caught.value.current_id == "f-m"


def test_a_state_the_caller_may_not_write_is_a_refusal():
    stored = _store([
        _closed("v1", "f-v2", lineage_id="L"), make_fact("v2", lineage_id="L", version=2, state=FactState.ARCHIVED),
    ])

    with pytest.raises(FactResolutionError, match="was replaced by f-v2, which is archived"):
        _run(stored, "f-v1", CONSOLIDATION_ACCEPTS)


def test_a_cycle_ends_in_the_hop_cap():
    stored = _store([_closed("a", "f-b", lineage_id="L"), _closed("b", "f-a", lineage_id="L", version=2)])

    with pytest.raises(FactResolutionError, match=f"more than {MAX_FORWARD_HOPS} forward hops"):
        _run(stored, "f-a", CABINET_ACCEPTS)


def test_feeding_a_finished_walk_is_a_bug():
    walk = ForwardWalk("f-x", ACC, CABINET_ACCEPTS)
    walk.feed(None)

    assert walk.wanted is None and walk.result is None
    with pytest.raises(ValueError):
        walk.feed(None)
