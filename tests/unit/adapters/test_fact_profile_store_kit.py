"""Fact profile store kit (fact profile RFC §2.1, §2.3, §4.2): `list_profile_states` and `write_fact_profile` checked
against their port docstrings, on `FirestoreFactRepository` over the evaluating Firestore fake with the index refusal ON
(`config/firestore.indexes.json`), and on the dry-run kit's overlay over it. Assertions are on the raw stored documents
and on what readers return. The Firestore behaviours they lean on (projection, transactions, vectors, arrays of maps)
are parity scenarios of `tests/support/firestore_parity.py`, run on the test database by `make test-cloud`.

Documents are seeded through `add_fact`, so every one carries its own `update_time`.
"""
import copy
from datetime import datetime, timezone

import pytest
from google.api_core.exceptions import Aborted
from google.cloud.firestore_v1.vector import Vector

from scripts.consolidation.s3_dry_run_kit import NoWriteFactRepository
from src.domain.entities import FactState
from src.domain.exceptions import InPlaceFieldRefused
from src.domain.fact_profile_fingerprint import profile_fingerprint
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=timezone.utc)
FP = "a" * 64
FP2 = "b" * 64
PROFILE = {
    "label_words": ["vehicle cover", "policy renewal"],
    "label_vector": embed("vehicle cover policy renewal"),
    "time_role": "validity",
    "time_points": [{"from": "2026-04-01", "to": "2027-03-31", "precision": "day", "source": "01/04/2026–31/03/2027"}],
    "place_keys": [],
    "places_unresolved": [{"city": "Nowhere", "country": "Italy"}],
    "people": [{"name": "Clara Moreira", "role": "partner", "name_keys": ["w:clara"], "role_keys": ["w:partner"]}],
    "primary_domain": "transport",
    "domain_keys": ["transport", "money"],
    "user_identity": False,
    "kinds": ["document"],
    "sensitivity": "ordinary",
    "profile_answer": {"subject": "transport", "money_flow": True},
    "profile_fingerprint": FP,
    "profile_derivation": "1|r|a|0",
    "profile_drops": {"label_repeats_text": 1},
    "profile_model": "model-x",
    "profiled_at": NOW,
}
STATE_FIELDS = ("profile_fingerprint", "profile_derivation", "profile_answer", "user_identity", "primary_domain",
                "sensitivity")


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


async def profiled(store, text, **over):
    fact = await seed(store, text, **over)
    assert await store.repo.write_fact_profile(fact.id, PROFILE, None)
    return fact


# ------------------------------------------------------------------------------------------------ the stored shape

async def test_an_unprofiled_fact_stores_its_profile_fields_empty(store):
    await seed(store, "plain")
    assert all(store.docs["f-plain"][name] is None for name in PROFILE)


async def test_a_written_profile_is_stored_flat_with_its_vector_and_read_back_whole(store):
    await profiled(store, "car")
    doc = store.docs["f-car"]
    assert isinstance(doc["label_vector"], Vector)
    assert doc["time_points"] == PROFILE["time_points"] and doc["people"] == PROFILE["people"]
    got = await store.repo.get_fact_by_id("f-car")
    assert got.label_vector == pytest.approx(PROFILE["label_vector"])
    assert (got.time_points, got.people, got.places_unresolved) == (
        PROFILE["time_points"], PROFILE["people"], PROFILE["places_unresolved"])
    assert (got.domain_keys, got.profile_drops, got.profiled_at) == (["transport", "money"], {"label_repeats_text": 1}, NOW)


async def test_a_content_version_carries_the_old_profile(store):
    old = await profiled(store, "car")
    stored = await store.repo.get_fact_by_id(old.id)
    version = stored.model_copy(update=dict(id="f-car-2", text="car, renewed", version=2))
    await store.repo.add_fact(version)
    doc = store.docs["f-car-2"]
    assert doc["profile_fingerprint"] == FP and isinstance(doc["label_vector"], Vector)


async def test_readers_without_vectors_leave_the_label_vector_out(store):
    await profiled(store, "car")
    (got,) = await store.repo.get_lineage(ACC, "lin-car")
    assert got.label_vector is None and got.vector is None and got.primary_domain == "transport"


# ------------------------------------------------------------------------------------------------ list_profile_states

async def test_list_profile_states_returns_the_current_facts_of_the_account_only(store, repo):
    await seed(store, "mine")
    await seed(store, "stale one", state=FactState.STALE)
    await seed(store, "theirs", account=OTHER)
    assert sorted(f.id for f in await repo.list_profile_states(ACC)) == ["f-mine"]


async def test_list_profile_states_projects_what_classification_reads_and_no_vector(store, repo):
    await profiled(store, "car", metadata={"policy": "MP-2026-118204"}, tags=["insurance"])
    (got,) = await repo.list_profile_states(ACC)
    assert (got.id, got.text, got.metadata, got.tags) == ("f-car", "car", {"policy": "MP-2026-118204"}, ["insurance"])
    assert got.domain is not None and got.created_at is not None
    assert {name: getattr(got, name) for name in STATE_FIELDS} == {name: PROFILE[name] for name in STATE_FIELDS}
    if isinstance(repo, NoWriteFactRepository):
        return      # the live list as the store returns it: checked on the store itself
    assert got.vector is None and got.label_vector is None
    assert (got.label_words, got.time_points, got.people, got.domain_keys) == (None, None, None, None)


async def test_list_profile_states_shows_the_failure_fields(store, repo):
    fact = await seed(store, "hard")
    assert await store.repo.write_fact_profile(fact.id, {"profile_failures": 2, "profile_failed_fingerprint": FP}, None)
    (got,) = await repo.list_profile_states(ACC)
    assert (got.profile_failures, got.profile_failed_fingerprint, got.profile_fingerprint) == (2, FP, None)


async def test_list_profile_states_of_an_empty_account_is_empty(repo):
    assert await repo.list_profile_states(ACC) == []


# ------------------------------------------------------------------------------------------------ write_fact_profile

async def test_the_first_profile_is_written_when_none_is_stored(store, repo):
    await seed(store, "car")
    assert await repo.write_fact_profile("f-car", PROFILE, None) is True
    got = await repo.get_fact_by_id("f-car")
    assert (got.profile_fingerprint, got.primary_domain) == (FP, "transport")


async def test_a_profile_is_rewritten_when_the_stored_fingerprint_is_the_expected_one(store, repo):
    await profiled(store, "car")
    assert await repo.write_fact_profile("f-car", {"profile_fingerprint": FP2, "kinds": []}, FP) is True
    got = await repo.get_fact_by_id("f-car")
    assert (got.profile_fingerprint, got.kinds, got.primary_domain) == (FP2, [], "transport")


@pytest.mark.parametrize("expected", [None, FP2])
async def test_a_lost_compare_and_set_writes_nothing(store, repo, expected):
    await profiled(store, "car")
    before = copy.deepcopy(store.docs)
    assert await repo.write_fact_profile("f-car", {**PROFILE, "profile_fingerprint": "c" * 64}, expected) is False
    assert store.docs == before
    assert (await repo.get_fact_by_id("f-car")).profile_fingerprint == FP


async def test_a_missing_document_is_not_written(store, repo):
    assert await repo.write_fact_profile("f-nobody", PROFILE, None) is False
    assert store.docs == {}


@pytest.mark.parametrize("field", ["text", "metadata", "domain", "state", "tags", "vector", "nonsense"])
async def test_a_field_outside_the_profile_is_refused_before_any_io(store, repo, field):
    await seed(store, "car")
    before = copy.deepcopy(store.docs)
    with pytest.raises(InPlaceFieldRefused) as caught:
        await repo.write_fact_profile("f-car", {"profile_fingerprint": FP, field: "x"}, None)
    assert caught.value.fields == (field,)
    assert store.docs == before


async def test_the_write_touches_only_the_given_fields(store):
    await seed(store, "car", metadata={"k": "v"})
    before = copy.deepcopy(store.docs["f-car"])
    await store.repo.write_fact_profile("f-car", {"profile_fingerprint": FP, "time_role": "state"}, None)
    after = store.docs["f-car"]
    written = ("profile_fingerprint", "time_role")
    assert {k: v for k, v in after.items() if k not in written} == {k: v for k, v in before.items() if k not in written}
    assert (after["profile_fingerprint"], after["time_role"]) == (FP, "state")


async def test_a_profile_written_meanwhile_by_another_run_wins(store):
    await seed(store, "car")
    store.interfere("f-car", {"profile_fingerprint": FP2}, after_read=True)   # lands after this run's read
    assert await store.repo.write_fact_profile("f-car", PROFILE, None) is False
    assert store.docs["f-car"]["profile_fingerprint"] == FP2
    assert store.docs["f-car"]["primary_domain"] is None


async def test_a_transaction_that_gives_up_on_contention_is_a_lost_write(store):
    await seed(store, "car")
    store.db.raise_on_txn_read(FACTS, "f-car", Aborted("contended"))
    assert await store.repo.write_fact_profile("f-car", PROFILE, None) is False
    assert store.docs["f-car"]["profile_fingerprint"] is None


async def test_a_text_unchanged_since_the_scan_keeps_the_fingerprint_it_was_profiled_with(store, repo):
    fact = await seed(store, "car", metadata={"policy": "MP-2026-118204"})
    fp = profile_fingerprint(fact.text, fact.metadata)
    (listed,) = await repo.list_profile_states(ACC)
    assert await repo.write_fact_profile(listed.id, {"profile_fingerprint": fp}, listed.profile_fingerprint)
    assert profile_fingerprint(listed.text, listed.metadata) == fp


# ------------------------------------------------------------------------------------------------ the dry-run kit

async def test_the_kit_writes_to_its_overlay_and_records_the_write(store):
    await seed(store, "car")
    kit = NoWriteFactRepository(store.repo)
    assert await kit.write_fact_profile("f-car", PROFILE, None)
    assert store.docs["f-car"]["profile_fingerprint"] is None
    assert kit.writes == [{"kind": "write_fact_profile", "fact_id": "f-car", "fields": sorted(PROFILE)}]
    (listed,) = await kit.list_profile_states(ACC)
    assert listed.profile_fingerprint == FP
    assert await kit.write_fact_profile("f-car", PROFILE, None) is False     # its own write moved the fingerprint
