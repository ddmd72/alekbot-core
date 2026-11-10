"""What the profile classification reads of a current fact now includes the open points of its profile (fact profile RFC
§4.7, PR 5e): `places_unresolved` and `profile_review_items` come back from `list_profile_states`, and the projection
adds nothing else (no vector, no time points, no people). The real repository over the evaluating fake; invented cast."""
from src.adapters.firestore_repo import _PROFILE_STATE_FIELDS, FirestoreFactRepository
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config

ROME_FACT = "Alex has a trip to Rome"


def test_the_projection_names_the_open_point_fields_once():
    assert _PROFILE_STATE_FIELDS.count("places_unresolved") == 1
    assert _PROFILE_STATE_FIELDS.count("profile_review_items") == 1
    assert not {"vector", "tags_vector", "metadata_vector", "label_vector", "time_points", "people"} & set(_PROFILE_STATE_FIELDS)


async def test_a_current_facts_open_points_are_read_with_its_profile_state():
    store = FactStore(indexes=INDEXES_FILE)
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder)
    await repo.add_fact(make_fact(ROME_FACT))
    store.docs[f"f-{ROME_FACT}"].update(
        places_unresolved=[{"city": "Rome", "country": ""}],
        profile_review_items=[{"field": "places", "mention": "Rome"}, {"field": "time_points", "mention": "2026"}],
        profile_review_needed=True, time_points=[{"from": "2026", "to": "", "precision": "year", "source": "2026"}])

    (fact,) = await repo.list_profile_states(ACC)

    assert fact.places_unresolved == [{"city": "Rome", "country": ""}]
    assert fact.profile_review_items == [{"field": "places", "mention": "Rome"},
                                         {"field": "time_points", "mention": "2026"}]
    assert fact.profile_review_needed is True
    assert not fact.time_points and fact.lineage_id == f"lin-{ROME_FACT}"


async def test_a_fact_without_open_points_reads_them_as_none():
    store = FactStore(indexes=INDEXES_FILE)
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder)
    await repo.add_fact(make_fact(ROME_FACT))

    (fact,) = await repo.list_profile_states(ACC)

    assert fact.places_unresolved is None and fact.profile_review_items is None
