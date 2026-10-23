"""FactRepository kit (P4 · T3): the docstrings of `src/ports/repository.py` checked against `FirestoreFactRepository` on the
evaluating Firestore fake, with the index refusal ON (`config/firestore.indexes.json`): a query no declared index serves fails
here. Assertions are on the raw stored documents and on what readers return. get_lineage, resolve_current,
write_superseding and update_fact_fields are tested in test_fact_repository_versions_kit.py; get_legacy_facts has its own
file (test_fact_repository_legacy_facts.py).

`store` is the one fixture that selects the backend.
"""
import copy
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import InvalidArgument
from google.cloud.firestore_v1.vector import Vector
from pydantic import ValidationError

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import (
    ContextPriority, ExpiryKind, FactDomain, FactState, FactVisibility, TemporalClass,
)
from src.domain.request_context import RequestContext
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import ACC, DROP, OTHER, T0, USER, as_legacy, make_fact, put_raw
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed, make_env_config
from tests.support.firestore_eval import stored_vector

CONTEXT_COLLECTION = "eval_user_context"


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def plain(doc):
    """A raw stored document with its vectors as lists, for comparing."""
    return {k: (stored_vector(v) if isinstance(v, Vector) else copy.deepcopy(v)) for k, v in doc.items()}


def ids(facts):
    return sorted(f.id for f in facts)


def _unit(theta):
    return Vector([math.cos(theta), math.sin(theta), 0.0])


# ------------------------------------------------------------------------------------------------ the readers

async def _active(s):
    return await s.repo.get_active_facts(ACC)


async def _ordered(s):
    return await s.repo.get_active_facts_ordered(ACC)


async def _longest(s):
    return await s.repo.get_longest_facts(ACC, 50)


async def _paginated(s):
    return (await s.repo.get_paginated_facts(ACC, limit=50))[0]


async def _by_domain(s):
    return await s.repo.search_facts_by_domain(["health"], limit=50, account_id=ACC)


async def _search(s):
    return await s.repo.search_facts(embed("query"), "vector", limit=50, user_id=USER, account_id=ACC)


ACCOUNT_READERS = pytest.mark.parametrize(
    "reader", [_active, _ordered, _longest, _paginated, _by_domain, _search],
    ids=["active", "ordered", "longest", "paginated", "by_domain", "search"],
)
ORDERED_BY_CREATED_AT = pytest.mark.parametrize(
    "reader", [_ordered, _paginated, _by_domain], ids=["ordered", "paginated", "by_domain"])
PROJECTION_READERS = pytest.mark.parametrize(
    "reader", [_active, _ordered, _longest], ids=["active", "ordered", "longest"])


# ------------------------------------------------------------------------- holds for every account-scoped read

@ACCOUNT_READERS
async def test_account_scoped_read_returns_only_current_facts_of_the_account(store, reader):
    put_raw(store, "cur", "own current")
    for state in ("stale", "archived", "superseded", "invalidated"):
        put_raw(store, state, state, state=state)
    put_raw(store, "no-state", "no state", state=DROP)
    put_raw(store, "foreign", "foreign current", account_id=OTHER)
    put_raw(store, "legacy", "legacy current", **as_legacy())
    assert ids(await reader(store)) == ["cur"]


@ACCOUNT_READERS
async def test_returned_fact_id_is_the_document_id(store, reader):
    put_raw(store, "doc-1", "one")
    put_raw(store, "doc-2", "two")
    assert ids(await reader(store)) == ["doc-1", "doc-2"]


@ACCOUNT_READERS
@pytest.mark.parametrize("bad", [{"type": "bogus"}, {"reported_date": "last week"}], ids=["unknown-enum", "bad-date"])
async def test_a_stored_document_that_does_not_validate_fails_the_whole_call(store, reader, bad):
    put_raw(store, "good", "good one")
    put_raw(store, "bad", "bad one", **bad)
    with pytest.raises(ValidationError):
        await reader(store)


@ORDERED_BY_CREATED_AT
async def test_a_document_missing_created_at_is_absent_from_a_read_ordered_by_it(store, reader):
    put_raw(store, "cur", "has created_at")
    put_raw(store, "no-date", "no created_at", created_at=DROP)
    assert ids(await reader(store)) == ["cur"]


# ------------------------------------------------------------------------------------------------- add_fact

async def test_add_fact_stores_the_whole_entity_under_its_id_and_returns_the_id(store):
    fact = make_fact("mother is Maria", tags=["family"], metadata={"name": "Maria"}, ttl_days=30,
                     context="said at lunch", version=3, replaces_fact_id="old-1", expires_at=T0 + timedelta(days=30),
                     temporal_class=TemporalClass.STABLE, context_priority=ContextPriority.HIGH,
                     visibility=FactVisibility.USER_PRIVATE)
    assert await store.repo.add_fact(fact) == fact.id
    doc = plain(store.docs[fact.id])
    expected = plain(fact.model_dump())
    expected["context_priority_rank"] = 2
    assert doc == expected
    assert all(isinstance(v, Vector) for v in (store.docs[fact.id][k] for k in ("vector", "tags_vector", "metadata_vector")))


async def test_add_fact_replaces_a_document_with_the_same_id_and_leaves_other_accounts_alone(store):
    put_raw(store, "f-x", "old text", legacy_note="left over")
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    foreign_before = copy.deepcopy(store.docs["foreign"])
    await store.repo.add_fact(make_fact("x", id="f-x"))
    assert sorted(store.docs) == ["f-x", "foreign"]
    assert store.docs["f-x"]["text"] == "x" and "legacy_note" not in store.docs["f-x"]
    assert store.docs["foreign"] == foreign_before


async def test_add_fact_stores_the_account_as_given(store):
    await store.repo.add_fact(make_fact("foreign-owned", account=OTHER))
    assert store.docs["f-foreign-owned"]["account_id"] == OTHER


# ------------------------------------------------------------------------------------------- get_fact_by_id

@pytest.mark.parametrize("state", list(FactState))
async def test_get_fact_by_id_returns_the_fact_in_any_state(store, state):
    put_raw(store, "f1", "any state", state=state.value)
    assert (await store.repo.get_fact_by_id("f1")).state == state


async def test_get_fact_by_id_returns_every_field_as_stored_with_vectors_as_lists(store):
    fact = make_fact("full", tags=["a"], metadata={"k": "v"}, ttl_days=30, context="said at lunch", version=3,
                     replaces_fact_id="old-1", expires_at=T0 + timedelta(days=30), temporal_class=TemporalClass.STABLE,
                     context_priority=ContextPriority.HIGH, visibility=FactVisibility.USER_PRIVATE,
                     reported_date=T0 - timedelta(days=1), state=FactState.ARCHIVED, valid_to=T0, is_current=True)
    await store.repo.add_fact(fact)
    read = await store.repo.get_fact_by_id(fact.id)
    assert read == fact
    assert all(type(v) is list for v in (read.vector, read.tags_vector, read.metadata_vector))


async def test_get_then_rewrite_preserves_every_field_of_the_document(store):
    # FactRepository.update_fact is gone (RFC delta D-4); add_fact is the remaining whole-document writer and shares
    # its stored form (`_to_document`) with write_superseding, so read -> write back must still lose nothing.
    fact = make_fact("full", tags=["a"], metadata={"k": "v"}, ttl_days=30, context="said at lunch", version=3,
                     replaces_fact_id="old-1", expires_at=T0 + timedelta(days=30), temporal_class=TemporalClass.STABLE,
                     context_priority=ContextPriority.LOW, visibility=FactVisibility.USER_PRIVATE,
                     state=FactState.STALE, superseded_by="next-1")
    await store.repo.add_fact(fact)
    before = plain(store.docs[fact.id])
    await store.repo.add_fact(await store.repo.get_fact_by_id(fact.id))
    assert plain(store.docs[fact.id]) == before


async def test_get_fact_by_id_has_no_tenancy_and_returns_none_for_a_missing_id(store):
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    assert (await store.repo.get_fact_by_id("foreign")).account_id == OTHER
    assert await store.repo.get_fact_by_id("missing") is None


async def test_get_fact_by_id_reads_a_legacy_owner_id_document_as_account_and_creator(store):
    put_raw(store, "old", "legacy", **as_legacy())
    put_raw(store, "old-private", "legacy private", **as_legacy(visibility="private"))
    put_raw(store, "old-no-visibility", "legacy shared", **as_legacy(visibility=DROP))
    old = await store.repo.get_fact_by_id("old")
    assert (old.account_id, old.created_by_user_id, old.visibility) == (ACC, ACC, FactVisibility.ACCOUNT_SHARED)
    assert (await store.repo.get_fact_by_id("old-private")).visibility == FactVisibility.USER_PRIVATE
    assert (await store.repo.get_fact_by_id("old-no-visibility")).visibility == FactVisibility.ACCOUNT_SHARED


# ------------------------------------------------------------------------------------------- get_active_facts

async def test_get_active_facts_has_no_limit(store):
    for i in range(101):
        await store.repo.add_fact(make_fact(f"fact {i}", vector=[0.1], tags_vector=[0.1], metadata_vector=[0.1]))
    assert len(await store.repo.get_active_facts(ACC)) == 101


async def test_get_active_facts_with_tags_returns_facts_carrying_at_least_one(store):
    for name, tags in (("a", ["anchor"]), ("b", ["x", "y"]), ("c", ["y"]), ("d", [])):
        await store.repo.add_fact(make_fact(name, tags=tags))
    await store.repo.add_fact(make_fact("foreign", account=OTHER, tags=["anchor"]))
    await store.repo.add_fact(make_fact("archived", tags=["anchor"], state=FactState.ARCHIVED))
    assert ids(await store.repo.get_active_facts(ACC, tags=["anchor", "y"])) == ["f-a", "f-b", "f-c"]


@PROJECTION_READERS
async def test_projection_reads_return_no_vectors_and_leave_the_omitted_fields_at_the_default(store, reader):
    fact = make_fact("full", tags=["a"], metadata={"k": "v"}, ttl_days=30, context="said at lunch", version=3,
                     replaces_fact_id="old-1", expires_at=T0 + timedelta(days=30), temporal_class=TemporalClass.STABLE,
                     context_priority=ContextPriority.HIGH, visibility=FactVisibility.USER_PRIVATE,
                     reported_date=T0 - timedelta(days=1), valid_to=T0 + timedelta(days=2), is_current=False,
                     age_days=3)
    await store.repo.add_fact(fact)
    (got,) = await reader(store)
    assert (got.vector, got.tags_vector, got.metadata_vector) == (None, None, None)
    assert (got.temporal_class, got.context, got.ttl_days, got.expires_at, got.replaces_fact_id) == (None,) * 5
    omitted = {"vector", "tags_vector", "metadata_vector", "temporal_class", "context", "ttl_days", "expires_at",
               "replaces_fact_id"}
    assert got.model_dump(exclude=omitted) == fact.model_dump(exclude=omitted)


# ----------------------------------------------------------------------------------- get_active_facts_ordered

async def test_ordered_by_priority_then_newest_first_and_archival_excluded(store):
    for text, priority, age in (("low-new", "low", 5), ("crit-old", "critical", 1), ("med-old", "medium", 1),
                                ("high-old", "high", 1), ("crit-new", "critical", 4), ("med-new", "medium", 3),
                                ("high-new", "high", 2), ("archival", "archival", 9), ("low-old", "low", 0)):
        await store.repo.add_fact(make_fact(text, age_days=age, context_priority=ContextPriority(priority)))
    got = await store.repo.get_active_facts_ordered(ACC)
    assert [f.text for f in got] == ["crit-new", "crit-old", "high-new", "high-old", "med-new", "med-old",
                                     "low-new", "low-old"]


async def test_ordered_domain_filter_and_limit_keep_the_head_of_the_order(store):
    for text, domain, priority, age in (("h1", "health", "high", 1), ("h2", "health", "critical", 0),
                                        ("h3", "health", "medium", 2), ("w1", "work", "critical", 3)):
        await store.repo.add_fact(make_fact(text, domain=FactDomain(domain), context_priority=ContextPriority(priority),
                                            age_days=age))
    assert [f.text for f in await store.repo.get_active_facts_ordered(ACC, domain="health")] == ["h2", "h1", "h3"]
    assert [f.text for f in await store.repo.get_active_facts_ordered(ACC, domain="health", limit=2)] == ["h2", "h1"]
    assert len(await store.repo.get_active_facts_ordered(ACC, limit=None)) == 4
    assert await store.repo.get_active_facts_ordered(ACC, domain="finance") == []


async def test_ordered_omits_a_document_without_a_priority_rank(store):
    put_raw(store, "ranked", "ranked")
    put_raw(store, "unranked", "unranked", context_priority_rank=DROP)
    assert ids(await store.repo.get_active_facts_ordered(ACC)) == ["ranked"]


# --------------------------------------------------------------------------------------- get_longest_facts

async def test_longest_facts_are_the_current_facts_with_the_most_words(store):
    for text in ("one", "one two", "one two three", "one two three four", "one two three four five"):
        await store.repo.add_fact(make_fact(text))
    await store.repo.add_fact(make_fact("a very very long fact of another account", account=OTHER))
    await store.repo.add_fact(make_fact("a very very long archived fact of this account", state=FactState.ARCHIVED))
    got = await store.repo.get_longest_facts(ACC, 3)
    assert [f.text for f in got] == ["one two three four five", "one two three four", "one two three"]
    assert await store.repo.get_longest_facts(ACC, 0) == []


# ------------------------------------------------------------------------------------- get_paginated_facts

async def _pages(store, limit, **kw):
    pages, cursor = [], None
    for _ in range(10):
        facts, cursor = await store.repo.get_paginated_facts(ACC, limit=limit, cursor_doc_id=cursor, **kw)
        pages.append([f.text for f in facts])
        if cursor is None:
            return pages
    raise AssertionError("the cursor never ended")


async def _five(store):
    for i in range(5):
        await store.repo.add_fact(make_fact(f"fact {i}", age_days=i))
    await store.repo.add_fact(make_fact("foreign", account=OTHER, age_days=9))
    await store.repo.add_fact(make_fact("archived", state=FactState.ARCHIVED, age_days=8))


async def test_first_page_is_newest_first_and_the_cursor_names_its_last_fact(store):
    await _five(store)
    facts, cursor = await store.repo.get_paginated_facts(ACC, limit=2)
    assert [f.text for f in facts] == ["fact 4", "fact 3"] and cursor == "f-fact 3"


async def test_next_cursor_is_none_exactly_when_no_fact_follows(store):
    await _five(store)
    assert await _pages(store, 5) == [["fact 4", "fact 3", "fact 2", "fact 1", "fact 0"]]
    assert await _pages(store, 6) == [["fact 4", "fact 3", "fact 2", "fact 1", "fact 0"]]
    _, cursor = await store.repo.get_paginated_facts(ACC, limit=4)
    assert cursor == "f-fact 1"


async def test_the_first_page_with_a_domain_holds_only_that_domain(store):
    for i in range(3):
        await store.repo.add_fact(make_fact(f"health {i}", age_days=i))
        await store.repo.add_fact(make_fact(f"work {i}", age_days=i, domain=FactDomain.WORK))
    facts, cursor = await store.repo.get_paginated_facts(ACC, limit=2, domain="work")
    assert [f.text for f in facts] == ["work 2", "work 1"] and cursor == "f-work 1"
    facts, cursor = await store.repo.get_paginated_facts(ACC, limit=3, domain="work")
    assert [f.text for f in facts] == ["work 2", "work 1", "work 0"] and cursor is None


async def test_pages_are_newest_first_do_not_overlap_and_hold_every_fact(store):
    await _five(store)
    assert await _pages(store, 2) == [["fact 4", "fact 3"], ["fact 2", "fact 1"], ["fact 0"]]
    assert await _pages(store, 4) == [["fact 4", "fact 3", "fact 2", "fact 1"], ["fact 0"]]


async def test_pages_with_a_domain_hold_only_that_domain(store):
    for i in range(3):
        await store.repo.add_fact(make_fact(f"health {i}", age_days=i))
        await store.repo.add_fact(make_fact(f"work {i}", age_days=i, domain=FactDomain.WORK))
    assert await _pages(store, 2, domain="work") == [["work 2", "work 1"], ["work 0"]]


async def test_an_unknown_cursor_id_restarts_at_the_first_page(store):
    await _five(store)
    first = await store.repo.get_paginated_facts(ACC, limit=2)
    again = await store.repo.get_paginated_facts(ACC, limit=2, cursor_doc_id="no-such-doc")
    assert ([f.id for f in again[0]], again[1]) == ([f.id for f in first[0]], first[1])


async def test_paginated_default_page_size_is_100(store):
    for i in range(101):
        await store.repo.add_fact(make_fact(f"fact {i}", age_days=i, vector=[0.1], tags_vector=[0.1], metadata_vector=[0.1]))
    facts, cursor = await store.repo.get_paginated_facts(ACC)
    assert len(facts) == 100 and cursor == facts[-1].id


# ------------------------------------------------------------------------------------------- search_facts

async def _spread(store):
    """Three own current facts: nearest first to e0 on `vector` is a,b,c; on `metadata_vector` c,b,a; on `tags_vector` b,c,a."""
    for name, v, m, t in (("a", 0.1, 0.9, 0.9), ("b", 0.4, 0.5, 0.1), ("c", 0.8, 0.1, 0.4)):
        put_raw(store, name, name, vector=_unit(v), metadata_vector=_unit(m), tags_vector=_unit(t))
    put_raw(store, "foreign", "foreign", account_id=OTHER, vector=_unit(0.0), metadata_vector=_unit(0.0),
            tags_vector=_unit(0.0))
    put_raw(store, "archived", "archived", state="archived", vector=_unit(0.0), metadata_vector=_unit(0.0),
            tags_vector=_unit(0.0))


@pytest.mark.parametrize("field,expected", [("vector", ["a", "b", "c"]), ("metadata_vector", ["c", "b", "a"]),
                                            ("tags_vector", ["b", "c", "a"])])
async def test_search_returns_current_facts_of_the_account_nearest_first_by_the_given_vector(store, field, expected):
    await _spread(store)
    got = await store.repo.search_facts([1.0, 0.0, 0.0], field, limit=10, user_id=USER, account_id=ACC)
    assert [f.id for f in got] == expected


async def test_search_returns_at_most_limit_nearest(store):
    await _spread(store)
    got = await store.repo.search_facts([1.0, 0.0, 0.0], "vector", limit=2, user_id=USER, account_id=ACC)
    assert [f.id for f in got] == ["a", "b"]


async def test_search_account_is_the_argument_else_the_context_account_else_the_user_id(store):
    put_raw(store, "own", "own")
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    query = [1.0, 0.0, 0.0]

    async def found(**kw):
        return ids(await store.repo.search_facts(query, "vector", **kw))

    assert await found(user_id=USER, account_id=OTHER) == ["foreign"]
    with RequestContext(user_id=USER, account_id=ACC):
        assert await found() == ["own"]
        assert await found(account_id=OTHER) == ["foreign"]
    with RequestContext(user_id=OTHER):
        assert await found() == ["foreign"]
    assert await found(user_id=ACC) == ["own"]


async def test_search_without_any_user_id_raises(store):
    put_raw(store, "own", "own")
    with pytest.raises(ValueError):
        await store.repo.search_facts([1.0, 0.0, 0.0], "vector", account_id=ACC)


async def test_search_projection_returns_vector_and_context_and_defaults_the_rest(store):
    # Reviewer edit (facts S3, T3; plan §1.1 / D1 "search results carry the current decision"): the select now carries
    # the stored expiry decision (expiry_kind, ttl_days, expires_at), so those three are returned, no longer defaulted.
    # The trail and stale_since stay out of the projection and must come back at their defaults.
    fact = make_fact("full", tags=["a"], metadata={"k": "v"}, context="said at lunch", ttl_days=30,
                     replaces_fact_id="old-1", expires_at=T0 + timedelta(days=30), temporal_class=TemporalClass.STABLE,
                     is_current=False, version=3, last_updated=T0, context_priority=ContextPriority.HIGH,
                     visibility=FactVisibility.USER_PRIVATE, reported_date=T0 - timedelta(days=1),
                     valid_to=T0 + timedelta(days=2), age_days=3, expiry_kind=ExpiryKind.HORIZON,
                     stale_since=T0, lifecycle_events=[{"at": T0, "from": None, "to": "current", "actor": "stage1",
                                                        "reason": "version 3 of old-1"}])
    await store.repo.add_fact(fact)
    (got,) = await store.repo.search_facts(fact.vector, "vector", user_id=USER, account_id=ACC)
    assert got.vector == fact.vector and got.context == "said at lunch"
    assert (got.expiry_kind, got.ttl_days, got.expires_at) == (ExpiryKind.HORIZON, 30, T0 + timedelta(days=30))
    assert (got.tags_vector, got.metadata_vector) == (None, None)
    assert (got.replaces_fact_id, got.stale_since, got.lifecycle_events) == (None, None, [])
    assert got.is_current is True
    assert datetime.now(timezone.utc) - got.last_updated < timedelta(minutes=1)
    defaulted = {"tags_vector", "metadata_vector", "replaces_fact_id", "is_current", "last_updated", "stale_since",
                 "lifecycle_events"}
    assert got.model_dump(exclude=defaulted) == fact.model_dump(exclude=defaulted)


async def test_search_backend_error_propagates(store):
    put_raw(store, "own", "own")
    store.db.collection(FACTS).fail = RuntimeError("backend down")
    with pytest.raises(RuntimeError):
        await store.repo.search_facts([1.0, 0.0, 0.0], "vector", user_id=USER, account_id=ACC)


async def test_search_returns_vectors_as_lists_of_floats(store):
    put_raw(store, "own", "own")
    (got,) = await store.repo.search_facts(embed("own"), "vector", user_id=USER, account_id=ACC)
    assert type(got.vector) is list and all(type(x) is float for x in got.vector)


# ------------------------------------------------------------------------------------ search_facts_by_domain

async def test_by_domain_returns_newest_first_within_limit_for_the_domains_only(store):
    for text, domain, age in (("h0", "health", 0), ("h1", "health", 1), ("w2", "work", 2), ("p3", "possession", 3),
                              ("h4", "health", 4)):
        await store.repo.add_fact(make_fact(text, domain=FactDomain(domain), age_days=age))
    await store.repo.add_fact(make_fact("foreign", account=OTHER, age_days=9))
    await store.repo.add_fact(make_fact("archived", state=FactState.ARCHIVED, age_days=8))
    got = await store.repo.search_facts_by_domain(["health", "work"], limit=3, account_id=ACC)
    assert [f.text for f in got] == ["h4", "w2", "h1"]
    assert len(await store.repo.search_facts_by_domain(["health"], account_id=ACC)) == 3


async def test_by_domain_account_is_the_argument_else_the_context_account_and_user_id_is_unused(store):
    put_raw(store, "own", "own")
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    assert ids(await store.repo.search_facts_by_domain(["health"], account_id=OTHER)) == ["foreign"]
    with RequestContext(user_id=OTHER, account_id=ACC):
        assert ids(await store.repo.search_facts_by_domain(["health"])) == ["own"]
        assert ids(await store.repo.search_facts_by_domain(["health"], user_id=OTHER)) == ["own"]
    with pytest.raises(ValueError):
        await store.repo.search_facts_by_domain(["health"], user_id=ACC)


async def test_by_domain_errors_and_empty_domains(store):
    put_raw(store, "own", "own")
    with pytest.raises(ValueError):
        await store.repo.search_facts_by_domain(["health"])
    assert await store.repo.search_facts_by_domain([], account_id=ACC) == []
    with pytest.raises(InvalidArgument):
        await store.repo.search_facts_by_domain([f"d{i}" for i in range(31)], account_id=ACC)
    assert ids(await store.repo.search_facts_by_domain([f"d{i}" for i in range(29)] + ["health"], account_id=ACC)) == ["own"]


# ------------------------------------------------------------------------------------------ add_fact_if_unique

def _near(similarity):
    """A vector whose cosine similarity with e0 is `similarity`."""
    return [similarity, math.sqrt(1 - similarity ** 2), 0.0]


async def _put(store, text, vector, **kw):
    fact = make_fact(text, vector=vector, **kw)
    await store.repo.add_fact(fact)
    return fact


async def test_unique_stores_a_fact_with_no_current_duplicate_and_returns_its_id(store):
    await _put(store, "likes tea", [1.0, 0.0, 0.0])
    new = make_fact("owns a car", vector=[0.0, 1.0, 0.0])
    assert await store.repo.add_fact_if_unique(new) == (True, new.id)
    assert sorted(store.docs) == ["f-likes tea", "f-owns a car"]


async def test_unique_skips_a_duplicate_and_returns_the_existing_id_without_writing(store):
    existing = await _put(store, "likes tea", [1.0, 0.0, 0.0])
    before = copy.deepcopy(store.docs)
    new = make_fact("likes tea", id="other-id", vector=[1.0, 0.0, 0.0])
    assert await store.repo.add_fact_if_unique(new) == (False, existing.id)
    assert store.docs == before


async def test_unique_judges_the_nearest_with_smart_deduplication(store):
    await _put(store, "weighs 70 kg", [1.0, 0.0, 0.0])
    new = make_fact("weighs 80 kg", vector=[1.0, 0.0, 0.0])
    assert await store.repo.add_fact_if_unique(new) == (True, new.id)


async def test_unique_threshold_bounds_the_distance_of_the_nearest(store):
    existing = await _put(store, "likes green tea", [1.0, 0.0, 0.0])
    new = make_fact("likes green tea", id="other-id", vector=_near(0.985))
    assert await store.repo.add_fact_if_unique(new, similarity_threshold=0.96) == (False, existing.id)
    assert await store.repo.add_fact_if_unique(new, similarity_threshold=0.99) == (True, "other-id")


async def test_unique_default_threshold_is_096(store):
    await _put(store, "likes green tea", [1.0, 0.0, 0.0])
    inside = make_fact("likes green tea", id="in", vector=_near(0.965))
    outside = make_fact("likes green tea", id="out", vector=_near(0.955))
    assert (await store.repo.add_fact_if_unique(outside))[0] is True
    assert (await store.repo.add_fact_if_unique(inside))[0] is False


async def test_unique_stores_a_fact_without_a_vector_unchecked(store):
    await _put(store, "likes tea", [1.0, 0.0, 0.0])
    new = make_fact("likes tea", id="no-vec", vector=None)
    assert await store.repo.add_fact_if_unique(new) == (True, "no-vec")
    assert store.docs["no-vec"]["vector"] is None


@pytest.mark.parametrize("kw", [{"account": OTHER}, {"state": FactState.ARCHIVED}, {"state": FactState.INVALIDATED},
                                {"state": FactState.SUPERSEDED}], ids=["other-account", "archived", "invalidated",
                                                                       "superseded"])
async def test_unique_compares_only_current_facts_of_the_facts_account(store, kw):
    await _put(store, "likes tea", [1.0, 0.0, 0.0], **kw)
    new = make_fact("likes tea", id="new-id", vector=[1.0, 0.0, 0.0])
    assert await store.repo.add_fact_if_unique(new) == (True, "new-id")


# ----------------------------------------------------------------------------- get_latest_fact_by_lineage

async def test_latest_by_lineage_returns_the_non_superseded_document_in_any_state(store):
    # S2 (decision 17, D-7): the latest document is the highest version whose state is not superseded, any other state
    # included; `is_current` is no longer read. Highest-version selection is pinned in test_fact_repository_versions_kit.py.
    put_raw(store, "a-elsewhere", "other lineage", entity={"lineage_id": "M"})
    put_raw(store, "b-old", "v1", entity={"lineage_id": "L"}, is_current=False, state="superseded")
    put_raw(store, "c-new", "v2", entity={"lineage_id": "L"}, is_current=True, state="archived")
    put_raw(store, "d-foreign", "v2 foreign", entity={"lineage_id": "L"}, account_id=OTHER)
    assert (await store.repo.get_latest_fact_by_lineage(ACC, "L")).id == "c-new"
    assert await store.repo.get_latest_fact_by_lineage(ACC, "none") is None
    assert (await store.repo.get_latest_fact_by_lineage(OTHER, "L")).id == "d-foreign"


async def test_latest_by_lineage_ignores_a_legacy_owner_id_document(store):
    # The owner_id fallback is dropped (RFC delta D-7, R3-M5: no live owner_id document, no caller): a legacy document
    # is absent from this account-scoped read, as from every other one (port docstring, "Legacy documents").
    put_raw(store, "a-elsewhere", "other lineage", entity={"lineage_id": "M"}, **as_legacy())
    put_raw(store, "b-old", "v1", entity={"lineage_id": "L"}, **as_legacy(is_current=False, state="superseded"))
    put_raw(store, "c-new", "v2", entity={"lineage_id": "L"}, **as_legacy())
    assert await store.repo.get_latest_fact_by_lineage(ACC, "L") is None
    assert await store.repo.get_latest_fact_by_lineage(OTHER, "L") is None


async def test_latest_by_lineage_returns_vectors_as_lists(store):
    put_raw(store, "new", "v2", entity={"lineage_id": "L"})
    got = await store.repo.get_latest_fact_by_lineage(ACC, "L")
    assert all(type(v) is list for v in (got.vector, got.tags_vector, got.metadata_vector))


# ------------------------------------------------------------------------------------------- invalidate_fact

async def test_invalidate_changes_state_valid_to_and_last_updated_only(store):
    await store.repo.add_fact(make_fact("x"))
    await store.repo.add_fact(make_fact("foreign", account=OTHER))
    foreign_before = copy.deepcopy(store.docs["f-foreign"])
    before = plain(store.docs["f-x"])
    started = datetime.now(timezone.utc)
    await store.repo.invalidate_fact("f-x", ACC)
    after = plain(store.docs["f-x"])
    # Reviewer edit (facts S3, T3; plan §3.8): the Cabinet invalidate writes its trail entry in the same write, so the
    # changed set gains lifecycle_events, and the entry itself is pinned.
    assert {k for k in after if after[k] != before[k]} == {"state", "valid_to", "last_updated", "lifecycle_events"}
    assert after["lifecycle_events"] == [{"at": after["valid_to"], "from": "current", "to": "invalidated",
                                          "actor": "cabinet", "reason": "deleted in the Cabinet"}]
    assert after["state"] == "invalidated" and type(after["state"]) is str
    assert started <= after["valid_to"] <= datetime.now(timezone.utc) and after["last_updated"] == after["valid_to"]
    assert after["is_current"] is True and after["version"] == before["version"]
    assert store.docs["f-foreign"] == foreign_before


async def test_invalidate_a_missing_id_raises_value_error_and_writes_nothing(store):
    put_raw(store, "other", "other")
    before = copy.deepcopy(store.docs)
    with pytest.raises(ValueError):
        await store.repo.invalidate_fact("missing", ACC)
    assert store.docs == before


async def test_invalidate_a_fact_of_another_account_or_a_legacy_one_raises_permission_error_and_writes_nothing(store):
    put_raw(store, "foreign", "foreign", account_id=OTHER)
    put_raw(store, "legacy", "legacy", **as_legacy())
    before = copy.deepcopy(store.docs)
    for fact_id in ("foreign", "legacy"):
        with pytest.raises(PermissionError):
            await store.repo.invalidate_fact(fact_id, ACC)
    assert store.docs == before


async def test_invalidate_twice_succeeds_and_moves_valid_to_and_last_updated(store):
    await store.repo.add_fact(make_fact("x"))
    await store.repo.invalidate_fact("f-x", ACC)
    first = copy.deepcopy(store.docs["f-x"])
    await store.repo.invalidate_fact("f-x", ACC)
    second = store.docs["f-x"]
    assert second["valid_to"] > first["valid_to"] and second["last_updated"] > first["last_updated"]
    assert second["state"] == "invalidated"


# ------------------------------------------------------------------------ the biographical context cache

def _cache(store, account=ACC):
    return store.db.collection(CONTEXT_COLLECTION).docs.get(account)


def _repo_with_cache_service(store):
    service = BiographicalContextService(None)
    repo = FirestoreFactRepository(store.db, make_env_config(), embedding_service=store.embedder,
                                   biographical_context_service=service)
    service.set_repository(repo)
    return repo


async def _seed_account_for_cache(store, account=ACC):
    for text, domain, priority, tags, age in (
            ("bio low", "biographical", "low", [], 3), ("bio critical", "biographical", "critical", [], 1),
            ("health high", "health", "high", [], 9), ("be calm", "biographical", "high", ["mindset"], 2),
            ("never apologise", "agent_directive", "critical", [], 4)):
        await store.repo.add_fact(make_fact(f"{account} {text}", account=account, domain=FactDomain(domain), tags=tags,
                                            context_priority=ContextPriority(priority), age_days=age))
    await store.repo.add_fact(make_fact(f"{account} archived", account=account, state=FactState.ARCHIVED))
    await store.repo.add_fact(make_fact(f"{account} archival", account=account, context_priority=ContextPriority.ARCHIVAL))


async def test_refresh_rebuilds_the_cache_from_the_current_facts(store):
    await _seed_account_for_cache(store)
    await _seed_account_for_cache(store, OTHER)
    repo = _repo_with_cache_service(store)
    started = datetime.now(timezone.utc)
    await repo.refresh_biographical_context_cache(ACC)
    cache = _cache(store)
    assert [f["text"] for f in cache["biographical_facts"]] == [f"{ACC} bio critical", f"{ACC} bio low",
                                                                f"{ACC} health high"]
    assert [f["text"] for f in cache["principles"]] == [f"{ACC} be calm"]
    assert [f["text"] for f in cache["directives"]] == [f"{ACC} never apologise"]
    assert (cache["facts_count"], cache["principles_count"], cache["version"]) == (3, 1, "v3_hexagonal_di")
    assert started <= cache["refreshed_at"] <= datetime.now(timezone.utc)
    assert set(cache["biographical_facts"][0]) == {"id", "text", "domain", "tags", "context_priority", "created_at"}
    assert _cache(store, OTHER) is None


async def test_refresh_of_an_unchanged_account_writes_the_same_content(store):
    await _seed_account_for_cache(store)
    repo = _repo_with_cache_service(store)
    await repo.refresh_biographical_context_cache(ACC)
    first = copy.deepcopy(_cache(store))
    await repo.refresh_biographical_context_cache(ACC)
    second = _cache(store)
    assert {k: v for k, v in second.items() if k != "refreshed_at"} == {k: v for k, v in first.items() if k != "refreshed_at"}


async def test_refresh_never_raises_and_keeps_the_previous_cache_on_a_failure(store):
    await _seed_account_for_cache(store)
    repo = _repo_with_cache_service(store)
    await repo.refresh_biographical_context_cache(ACC)
    before = copy.deepcopy(_cache(store))
    store.db.collection(FACTS).fail = RuntimeError("backend down")
    await repo.refresh_biographical_context_cache(ACC)
    assert _cache(store) == before


async def test_refresh_without_a_context_service_does_nothing(store):
    await _seed_account_for_cache(store)
    await store.repo.refresh_biographical_context_cache(ACC)
    assert _cache(store) is None


async def test_refresh_holds_at_most_facts_limit_facts(store):
    for i in range(4):
        await store.repo.add_fact(make_fact(f"bio {i}", domain=FactDomain.BIOGRAPHICAL, age_days=i))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC, facts_limit=2)
    assert _cache(store)["facts_count"] == 2


async def test_refresh_keeps_directives_separately_up_to_15_highest_priority_first(store):
    for i in range(15):
        priority = ContextPriority.CRITICAL if i < 8 else ContextPriority.HIGH
        await store.repo.add_fact(make_fact(f"directive {i}", domain=FactDomain.AGENT_DIRECTIVE,
                                            context_priority=priority, age_days=i))
    await store.repo.add_fact(make_fact("directive low", domain=FactDomain.AGENT_DIRECTIVE,
                                        context_priority=ContextPriority.LOW, age_days=99))
    await _repo_with_cache_service(store).refresh_biographical_context_cache(ACC)
    cache = _cache(store)
    assert len(cache["directives"]) == 15 and "directive low" not in [d["text"] for d in cache["directives"]]
    assert (cache["facts_count"], cache["principles_count"]) == (0, 0)


async def test_cached_context_is_facts_then_principles_then_directives_with_iso_dates_and_ignores_limit(store):
    created = datetime(2026, 2, 3, 4, 5, 6, tzinfo=timezone.utc)
    store.db.collection(CONTEXT_COLLECTION).docs[ACC] = {
        "biographical_facts": [{"id": "f1", "text": "fact", "created_at": created}, {"id": "f2", "text": "fact 2"}],
        "principles": [{"id": "p1", "text": "principle", "created_at": "2026-01-01T00:00:00+00:00"}],
        "directives": [{"id": "d1", "text": "directive"}],
    }
    store.db.collection(CONTEXT_COLLECTION).docs[OTHER] = {"biographical_facts": [{"id": "x", "text": "foreign"}]}
    got = await store.repo.get_biographical_context_cached(ACC, limit=1)
    assert [f["id"] for f in got] == ["f1", "f2", "p1", "d1"]
    assert got[0]["created_at"] == created.isoformat() and got[2]["created_at"] == "2026-01-01T00:00:00+00:00"


async def test_cached_context_owner_none_resolves_the_context_account_and_no_account_returns_empty(store):
    store.db.collection(CONTEXT_COLLECTION).docs[ACC] = {"biographical_facts": [{"id": "f1", "text": "fact"}]}
    store.db.collection(CONTEXT_COLLECTION).docs[OTHER] = {"biographical_facts": [{"id": "x", "text": "foreign"}]}
    assert await store.repo.get_biographical_context_cached() == []
    with RequestContext(user_id=OTHER, account_id=ACC):
        assert [f["id"] for f in await store.repo.get_biographical_context_cached()] == ["f1"]
    with RequestContext(user_id=OTHER):
        assert [f["id"] for f in await store.repo.get_biographical_context_cached()] == ["x"]


async def test_cached_context_missing_or_empty_cache_returns_empty(store):
    assert await store.repo.get_biographical_context_cached(ACC) == []
    store.db.collection(CONTEXT_COLLECTION).docs[ACC] = {"biographical_facts": [], "principles": [], "directives": []}
    assert await store.repo.get_biographical_context_cached(ACC) == []


async def test_cached_context_raises_on_a_backend_failure(store, monkeypatch):
    def boom(_doc_id):
        raise RuntimeError("backend down")

    monkeypatch.setattr(store.repo.user_context_col, "document", boom)
    with pytest.raises(RuntimeError):
        await store.repo.get_biographical_context_cached(ACC)
