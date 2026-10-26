"""`ForwardWalk` (S4 T2c, hexagonal review H1; sans-I/O since T12d): the one forward walk behind `resolve_current`. The same cases run over a
plain dict reader, through the Firestore repository's reader (the evaluating fake) and through the dry-run kit's overlay
reader, and must give the same document or the same refusal text."""
from typing import Dict, List, Optional

import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactEntity, FactState
from src.domain.exceptions import FactResolutionError
from src.domain.fact_lifecycle import CABINET_ACCEPTS, CONSOLIDATION_ACCEPTS
from src.domain.fact_resolution import ForwardWalk
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, OTHER, T0, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore


async def resolve_chain(read, start_id, account_id, accepted):
    """The loop every store runs over its own reader."""
    walk = ForwardWalk(start_id, account_id, accepted)
    while walk.wanted is not None:
        walk.feed(await read(walk.wanted))
    return walk.result


def _closed(name: str, successor: str, **over) -> FactEntity:
    return make_fact(name, state=FactState.SUPERSEDED, is_current=False, valid_to=T0, superseded_by=successor, **over)


def _world() -> List[FactEntity]:
    return [
        make_fact("x"),                                                     # direct hit
        _closed("v1", "f-v2", lineage_id="L"),                              # followed hit: v1 -> v2 -> v3
        _closed("v2", "f-v3", lineage_id="L", version=2),
        make_fact("v3", lineage_id="L", version=3, state=FactState.ARCHIVED),
        _closed("lost", "f-gone", lineage_id="LL"),                         # successor unknown
        _closed("a", "f-m", lineage_id="LA"),                               # merged into another lineage
        make_fact("m", lineage_id="LM"),
        make_fact("foreign", account=OTHER),                                # another account
    ]


# start id, accepted states -> the id it ends on, or the refusal text; None = no document for this account
CASES = [
    ("f-x", CABINET_ACCEPTS, "f-x"),
    ("f-v1", CABINET_ACCEPTS, "f-v3"),
    ("f-v2", CABINET_ACCEPTS, "f-v3"),
    ("f-v1", CONSOLIDATION_ACCEPTS, FactResolutionError("fact f-v1 was replaced by f-v3, which is archived")),
    ("f-lost", CABINET_ACCEPTS, FactResolutionError("fact f-lost was superseded and its successor is unknown; search again")),
    ("f-a", CABINET_ACCEPTS, FactResolutionError("fact f-a was merged into f-m; search again")),
    ("f-foreign", CABINET_ACCEPTS, None),
    ("f-missing", CABINET_ACCEPTS, None),
]


def _dict_reader(facts: List[FactEntity]):
    stored: Dict[str, StoredFact] = {f.id: StoredFact(f, f"token-{f.id}") for f in facts}

    async def read(fact_id: str) -> Optional[StoredFact]:
        return stored.get(fact_id)

    return read


async def _outcome(call):
    try:
        got = await call
    except FactResolutionError as exc:
        return ("refused", str(exc), exc.current_id)
    return None if got is None else ("found", got.fact.id, got.fact.text)


def _expected(want):
    if want is None:
        return None
    if isinstance(want, FactResolutionError):
        return ("refused", str(want))
    return ("found", want)


def _shape(outcome):
    return None if outcome is None else outcome[:2]


@pytest.fixture
async def store():
    store = FactStore(indexes=INDEXES_FILE)
    for fact in _world():
        await store.repo.add_fact(fact)
    return store


@pytest.mark.parametrize("start, accepted, want", CASES)
async def test_the_walk_over_a_dict_reader(start, accepted, want):
    outcome = await _outcome(resolve_chain(_dict_reader(_world()), start, ACC, accepted))

    assert _shape(outcome) == _expected(want)


@pytest.mark.parametrize("start, accepted, want", CASES)
async def test_the_repository_and_the_kit_walk_as_the_dict_reader_does(store, start, accepted, want):
    by_dict = await _outcome(resolve_chain(_dict_reader(_world()), start, ACC, accepted))
    by_repo = await _outcome(store.repo.resolve_current(start, ACC, accepted))
    by_kit = await _outcome(NoWriteFactRepository(store.repo).resolve_current(start, ACC, accepted))

    assert by_repo == by_dict and by_kit == by_dict     # same document and text, or the same refusal and current_id
    assert _shape(by_repo) == _expected(want)


async def test_the_repository_returns_the_change_token_of_the_document_it_ends_on(store):
    got = await store.repo.resolve_current("f-v1", ACC, CABINET_ACCEPTS)

    assert got.fact.id == "f-v3" and got.update_time == store.db.collection("eval_domain_facts").versions["f-v3"]


async def test_the_kit_walk_follows_its_own_overlay_before_the_live_store(store):
    kit = NoWriteFactRepository(store.repo)
    await kit.add_fact(make_fact("v4", lineage_id="L", version=4))
    v3 = await kit.resolve_current("f-v3", ACC, CABINET_ACCEPTS)
    await kit.update_fact_fields(v3, {"state": FactState.SUPERSEDED, "is_current": False, "superseded_by": "f-v4"})

    by_kit = await kit.resolve_current("f-v1", ACC, CABINET_ACCEPTS)
    by_repo = await store.repo.resolve_current("f-v1", ACC, CABINET_ACCEPTS)

    assert by_kit.fact.id == "f-v4" and by_repo.fact.id == "f-v3"
    assert "f-v4" not in store.docs and store.docs["f-v3"]["state"] == "archived"   # the live store is untouched
