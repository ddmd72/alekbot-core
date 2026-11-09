"""Fact search store kit (fact profile RFC §5.2): `list_search_snapshot`, `get_baseline_refreshed_at`, `nearest_fact_ids`
and `get_fact_vectors` checked against their port docstrings, on `FirestoreFactRepository` over the evaluating Firestore
fake with the index refusal ON, and on the dry-run kit's delegation over it. The Firestore behaviours they lean on
(projection, `find_nearest`, `get_all` with `field_paths`) are parity scenarios of `tests/support/firestore_parity.py`.
"""
from datetime import datetime, timezone

import pytest

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactState
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, embed

REFRESHED = datetime(2026, 11, 9, 8, 30, tzinfo=timezone.utc)
PROFILE = dict(label_words=["vehicle cover"], time_role="validity", primary_domain="transport",
               domain_keys=["transport", "money"], kinds=["document"], sensitivity="ordinary", place_keys=["pl:1"],
               time_points=[{"from": "2026-04-01", "to": "2027-03-31", "precision": "day", "source": "x"}],
               people=[{"name": "Clara Moreira", "role": "partner", "name_keys": [], "role_keys": []}],
               label_vector=embed("label car"), profile_fingerprint="a" * 64)


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


@pytest.fixture(params=["firestore", "kit"])
def repo(request, store):
    return store.repo if request.param == "firestore" else NoWriteFactRepository(store.repo)


async def seed(store, text, **over):
    fact = make_fact(text, **over)
    await store.repo.add_fact(fact)
    return fact


# ------------------------------------------------------------------------------------------------ list_search_snapshot

async def test_the_snapshot_lists_the_current_facts_of_the_account_only(store, repo):
    await seed(store, "mine")
    await seed(store, "stale one", state=FactState.STALE)
    await seed(store, "theirs", account=OTHER)
    assert sorted(f.id for f in await repo.list_search_snapshot(ACC)) == ["f-mine"]
    assert await repo.list_search_snapshot("nobody") == []


async def test_the_snapshot_projects_text_metadata_dates_and_the_profile_without_vectors(store, repo):
    await seed(store, "car", metadata={"policy": "MP-2026-118204"}, tags=["insurance"], context="renewal", **PROFILE)
    (got,) = await repo.list_search_snapshot(ACC)
    assert (got.id, got.text, got.metadata, got.tags, got.context) == (
        "f-car", "car", {"policy": "MP-2026-118204"}, ["insurance"], "renewal")
    assert got.reported_date is not None and got.created_at is not None and got.domain is not None
    assert (got.label_words, got.time_role, got.primary_domain, got.domain_keys, got.kinds, got.sensitivity,
            got.place_keys, got.time_points, got.people) == (
        PROFILE["label_words"], "validity", "transport", ["transport", "money"], ["document"], "ordinary", ["pl:1"],
        PROFILE["time_points"], PROFILE["people"])
    assert got.vector is None and got.label_vector is None and got.tags_vector is None


async def test_the_snapshot_leaves_the_fields_it_does_not_project_at_their_defaults(store):
    await seed(store, "car", **PROFILE)
    (got,) = await store.repo.list_search_snapshot(ACC)
    assert got.profile_fingerprint is None and got.profile_answer is None and got.user_identity is None


async def test_an_unprofiled_fact_reads_with_empty_profile_fields(store, repo):
    await seed(store, "plain")
    (got,) = await repo.list_search_snapshot(ACC)
    assert (got.label_words, got.time_points, got.people, got.domain_keys, got.sensitivity) == (None,) * 5


# ------------------------------------------------------------------------------------------------ baseline

async def test_the_baseline_token_is_the_refreshed_at_of_the_cache_document(store, repo):
    await store.db.collection("eval_user_context").document(ACC).set({"refreshed_at": REFRESHED, "facts_count": 3})
    assert await repo.get_baseline_refreshed_at(ACC) == REFRESHED


async def test_there_is_no_token_without_a_document_or_without_the_field(store, repo):
    assert await repo.get_baseline_refreshed_at(ACC) is None
    await store.db.collection("eval_user_context").document(ACC).set({"facts_count": 3})
    assert await repo.get_baseline_refreshed_at(ACC) is None


async def test_the_token_is_per_account(store, repo):
    await store.db.collection("eval_user_context").document(OTHER).set({"refreshed_at": REFRESHED})
    assert await repo.get_baseline_refreshed_at(ACC) is None


# ------------------------------------------------------------------------------------------------ nearest_fact_ids

async def test_nearest_text_vectors_come_nearest_first_up_to_the_limit(store, repo):
    for text in ("alpha", "beta", "gamma", "delta"):
        await seed(store, text)
    got = await repo.nearest_fact_ids(ACC, "vector", embed("gamma"), 3)
    assert len(got) == 3 and got[0] == "f-gamma"
    assert await repo.nearest_fact_ids(ACC, "vector", embed("gamma"), 1) == ["f-gamma"]


async def test_nearest_label_vectors_search_the_label_field_and_skip_unprofiled_facts(store, repo):
    await seed(store, "car", **PROFILE)
    await seed(store, "plain")
    assert await repo.nearest_fact_ids(ACC, "label_vector", embed("label car"), 10) == ["f-car"]


async def test_nearest_is_tenant_and_state_scoped(store, repo):
    await seed(store, "mine")
    await seed(store, "stale", state=FactState.STALE)
    await seed(store, "theirs", account=OTHER)
    assert await repo.nearest_fact_ids(ACC, "vector", embed("theirs"), 10) == ["f-mine"]


@pytest.mark.parametrize("field", ["tags_vector", "metadata_vector", "text", ""])
async def test_nearest_refuses_any_other_field_before_any_read(store, repo, field):
    with pytest.raises(ValueError):
        await repo.nearest_fact_ids(ACC, field, embed("x"), 3)


async def test_nearest_returns_ids_only_so_no_vector_travels(store):
    await seed(store, "car", **PROFILE)
    issued = store.db.collection("eval_domain_facts").executed
    await store.repo.nearest_fact_ids(ACC, "vector", embed("car"), 5)
    selects = [op[1] for op in issued[-1] if op[0] == "select"]
    assert selects and all("vector" not in path for path in selects[0])


# ------------------------------------------------------------------------------------------------ get_fact_vectors

async def test_vectors_come_back_by_id_for_the_account_only(store, repo):
    await seed(store, "one")
    await seed(store, "two")
    await seed(store, "theirs", account=OTHER)
    got = await repo.get_fact_vectors(ACC, ["f-one", "f-two", "f-theirs", "f-missing"])
    assert sorted(got) == ["f-one", "f-two"]
    assert got["f-one"] == pytest.approx(embed("one"))


async def test_a_fact_without_a_text_vector_is_left_out(store, repo):
    await seed(store, "one")
    await seed(store, "bare", vector=None)
    assert sorted(await repo.get_fact_vectors(ACC, ["f-one", "f-bare"])) == ["f-one"]


async def test_an_empty_request_reads_nothing_and_a_long_one_comes_back_whole(store, repo):
    for n in range(305):
        await seed(store, f"fact {n}")
    assert await repo.get_fact_vectors(ACC, []) == {}
    ids = [f"f-fact {n}" for n in range(305)]
    assert sorted(await repo.get_fact_vectors(ACC, ids)) == sorted(ids)


async def test_vectors_are_fetched_with_the_two_field_paths_only(store, monkeypatch):
    await seed(store, "one")
    seen = []
    original = store.db.get_all

    def spy(refs, field_paths=None, transaction=None):
        seen.append(field_paths)
        return original(refs, field_paths=field_paths, transaction=transaction)

    monkeypatch.setattr(store.db, "get_all", spy)
    await store.repo.get_fact_vectors(ACC, ["f-one"])
    assert seen == [["account_id", "vector"]]
