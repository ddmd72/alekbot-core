"""Facts S2 · T4: `FactManagementAdapter.update_fact` and `merge_facts` as version writes and one atomic merge, through the
real adapter, write service and repository on the evaluating Firestore fake with the index refusal ON (§10.2).

Assertions are on the raw stored documents and on the returned dicts. Every concurrency test names its interleaving source
(a slow embedder on the adapter and, for a merge, on the write service too; `store.interfere`) and asserts the interleaving
happened (a recorded write conflict, embedder call counts, the interfering write's field).
"""
import asyncio
import copy
import json
from datetime import datetime, timezone

import pytest

from src.adapters.firestore_repo import FirestoreFactRepository
from src.domain.entities import FactEntity
from src.domain.exceptions import FactWriteConflict
from src.domain.fact_embedding import fact_embedding_texts
from src.domain.fact_update_plan import FactUpdatePlan
from src.domain.stored_fact import StoredFact
from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_store import FACTS, INDEXES_FILE, FactStore, embed, make_env_config
from tests.support.firestore_eval import stored_vector

STALE_DATE = datetime(2020, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
VECTORS = ("vector", "tags_vector", "metadata_vector")


@pytest.fixture
def store():
    return FactStore(indexes=INDEXES_FILE)


def meta(**over):
    base = {
        "account_id": ACC, "user_id": USER, "domain": "biographical", "temporal_class": "stable",
        "context_priority": "medium", "tags": ["family", "mother"], "metadata": {"name": "Valentina"}, "type": "event",
    }
    base.update(over)
    return base


async def create(store, text="mother is Valentina", **over):
    res = await store.adapter.create_fact(text, meta(**over))
    assert res["status"] == "created", res
    return res["fact_id"]


def snapshot(store):
    return copy.deepcopy(store.docs)


def stamp(store, doc_id):
    return store.db.collection(FACTS).versions[doc_id]


def currents(store, lineage_id):
    return [d for d in store.docs.values() if d["lineage_id"] == lineage_id and d["state"] == "current"]


class SlowEmbedder:
    """Delegates to the hash embedder after a delay; counts the batch calls."""

    def __init__(self, inner, delay):
        self._inner, self._delay, self.calls = inner, delay, 0

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls += 1
        await asyncio.sleep(self._delay)
        return await self._inner.get_embeddings_batch(texts, task_type)


class FixedEmbedder:
    def __init__(self, vectors):
        self._vectors = vectors

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        return self._vectors


class FailingEmbedder:
    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        raise RuntimeError("embedding service down")


class GatedEmbedder:
    """Delegates to the hash embedder; on each call sets `signal` (if any), then waits for `gate` (if any; 5 s guard).

    Orders a gathered race by events instead of sleep margins (reviewer, F1 follow-up)."""

    def __init__(self, inner, gate=None, signal=None):
        self._inner, self._gate, self._signal, self.calls = inner, gate, signal, 0

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.calls += 1
        if self._signal is not None:
            self._signal.set()
        if self._gate is not None:
            await asyncio.wait_for(self._gate.wait(), timeout=5)
        return await self._inner.get_embeddings_batch(texts, task_type)


class WriteOutcomes:
    """Records how each call of a repository write method ended ("ok" or "conflict"); sets `on_ok` after a commit."""

    def __init__(self, repo, name, on_ok=None):
        original = getattr(repo, name)
        self.outcomes = []

        async def recording(*args, **kwargs):
            try:
                result = await original(*args, **kwargs)
            except FactWriteConflict:
                self.outcomes.append("conflict")
                raise
            self.outcomes.append("ok")
            if on_ok is not None:
                on_ok.set()
            return result

        setattr(repo, name, recording)


def count_resolutions(repo):
    calls = []
    original = repo.resolve_current

    async def counting(fact_id, account_id, accepted):
        calls.append(fact_id)
        return await original(fact_id, account_id, accepted)

    repo.resolve_current = counting
    return calls


# ------------------------------------------------------------------------------------------ versions and in place

async def test_content_update_writes_a_new_version_and_closes_the_old_in_one_commit(store):
    fid = await create(store, reported_date="2026-03-04T10:00:00Z")
    old = copy.deepcopy(store.docs[fid])

    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC)

    new_id = res["fact_id"]
    assert new_id != fid and res["status"] == "updated" and res["version"] == 2
    new = store.docs[new_id]
    assert (new["lineage_id"], new["version"], new["replaces_fact_id"]) == (old["lineage_id"], 2, fid)
    assert new["created_at"] == old["created_at"] and new["valid_from"] > old["valid_from"]
    assert (new["state"], new["is_current"], new["valid_to"], new["superseded_by"]) == ("current", True, None, None)
    assert new["text"] == "mother is Valentyna" and new["account_id"] == ACC
    for field, text in zip(VECTORS, fact_embedding_texts("mother is Valentyna", new["tags"], new["metadata"])):
        assert stored_vector(new[field]) == pytest.approx(embed(text))
    closed = store.docs[fid]
    assert (closed["state"], closed["is_current"], closed["superseded_by"]) == ("superseded", False, new_id)
    assert closed["valid_to"] is not None and closed["text"] == old["text"] and closed["version"] == 1
    assert stamp(store, new_id) == stamp(store, fid)


async def test_version_is_found_by_every_reader_after_content_and_after_temporal_class_only_update(store):
    fid = await create(store)
    first = (await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC))["fact_id"]
    second = (await store.adapter.update_fact(first, {"temporal_class": "DYNAMIC"}, ACC))["fact_id"]
    assert len({fid, first, second}) == 3

    active = await store.repo.get_active_facts_ordered(ACC)
    assert [f.id for f in active] == [second]
    texts = fact_embedding_texts("mother is Valentyna", ["family", "mother", "consolidated"], {"name": "Valentina"})
    for field, text in zip(VECTORS, texts):
        found = await store.repo.search_facts(embed(text), field, 5, user_id=USER, account_id=ACC)
        assert [f.id for f in found] == [second], field
    doc = store.docs[second]
    assert doc["temporal_class"] == "dynamic" and doc["version"] == 3 and doc["replaces_fact_id"] == first
    for field, text in zip(VECTORS, texts):
        assert stored_vector(doc[field]) == pytest.approx(embed(text))


async def test_temporal_class_only_update_is_a_version(store):
    fid = await create(store)
    before = snapshot(store)[fid]

    res = await store.adapter.update_fact(fid, {"temporal_class": "dynamic"}, ACC)

    assert res["fact_id"] != fid and res["version"] == 2 and len(store.docs) == 2
    new = store.docs[res["fact_id"]]
    assert new["temporal_class"] == "dynamic" and new["text"] == before["text"]
    for field in VECTORS:
        assert stored_vector(new[field]) == stored_vector(before[field])
    assert store.docs[fid]["state"] == "superseded" and store.docs[fid]["superseded_by"] == res["fact_id"]


@pytest.mark.parametrize("updates", [
    {"state": "ARCHIVED"},
    {"reported_date": "2020-01-02T03:04:05Z"},
    {},
])
async def test_state_only_reported_date_only_and_empty_updates_stay_in_place_with_version_unchanged(store, updates):
    fid = await create(store)
    before = snapshot(store)[fid]

    res = await store.adapter.update_fact(fid, updates, ACC)

    assert res["fact_id"] == fid and res["status"] == "updated" and res["version"] == 1
    assert list(store.docs) == [fid]
    doc = store.docs[fid]
    assert doc["version"] == 1 and doc["last_updated"] > before["last_updated"]
    assert doc["text"] == before["text"] and doc["replaces_fact_id"] is None
    for field in VECTORS:
        assert stored_vector(doc[field]) == stored_vector(before[field])
    if "state" in updates:
        assert doc["state"] == "archived" and doc["valid_to"] is not None
    if "reported_date" in updates:
        assert doc["reported_date"] == STALE_DATE


# ------------------------------------------------------------------------------------------------- resolution

async def test_second_update_on_a_superseded_id_lands_on_the_current_version(store):
    fid = await create(store)
    v2 = (await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC))["fact_id"]

    v3 = await store.adapter.update_fact(fid, {"content": "mother is Valentyna Ivanivna"}, ACC)
    assert v3["status"] == "updated" and v3["version"] == 3 and v3["fact_id"] not in (fid, v2)
    assert store.docs[v3["fact_id"]]["replaces_fact_id"] == v2 and store.docs[v2]["superseded_by"] == v3["fact_id"]
    assert store.docs[fid]["superseded_by"] == v2

    # Reviewer edit (facts S3, T2): the in-place state is ARCHIVED; the model may no longer send STALE (D1).
    in_place = await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    assert in_place["fact_id"] == v3["fact_id"] and in_place["version"] == 3
    assert store.docs[v3["fact_id"]]["state"] == "archived" and len(store.docs) == 3


# Reviewer edit (facts S3, T2): the in-place case is ARCHIVED; the model may no longer send STALE (D1).
@pytest.mark.parametrize("updates", [{"content": "mother is Valentyna Ivanivna"}, {"state": "ARCHIVED"}])
async def test_an_update_forwarded_to_the_current_version_says_so(store, updates):
    fid = await create(store)
    v2 = (await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC))["fact_id"]

    res = await store.adapter.update_fact(fid, updates, ACC)

    assert set(res) == {"fact_id", "status", "version", "message", "resolved_from"}
    assert res["status"] == "updated" and res["resolved_from"] == fid and res["fact_id"] != fid
    assert v2 in res["message"] and "current version" in res["message"]
    json.dumps(res)


async def test_an_update_on_the_current_id_carries_no_resolved_from(store):
    fid = await create(store)

    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC)

    assert set(res) == {"fact_id", "status", "version", "message"} and res["message"] == "Fact updated successfully"


async def test_update_on_a_merged_source_is_a_failed_result_naming_the_merged_id(store):
    a = await create(store, "likes tea")
    merged = (await store.adapter.merge_facts([a], "likes tea daily", meta()))["new_fact_id"]
    before = snapshot(store)

    res = await store.adapter.update_fact(a, {"content": "likes black tea"}, ACC)

    assert res["status"] == "failed" and res["fact_id"] == a
    assert merged in res["message"] and "merged" in res["message"]
    assert store.docs == before


async def test_update_of_a_foreign_merged_id_is_not_found(store):
    foreign = await create(store, "foreign fact", account_id=OTHER, user_id="usr-2")
    merged = (await store.adapter.merge_facts(
        [foreign], "foreign merged", meta(account_id=OTHER, user_id="usr-2")))["new_fact_id"]
    before = snapshot(store)

    res = await store.adapter.update_fact(foreign, {"content": "hijack"}, ACC)

    assert res == {"fact_id": foreign, "status": "not_found", "message": "Fact not found"}
    assert merged not in str(res) and store.docs == before


@pytest.mark.parametrize("updates", [{"content": "edited"}, {"state": "CURRENT"}])
async def test_update_on_an_archived_fact_is_refused(store, updates):
    fid = await create(store)
    await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)
    before = snapshot(store)

    res = await store.adapter.update_fact(fid, updates, ACC)

    assert res["status"] == "failed" and "archived" in res["message"] and res["fact_id"] == fid
    assert store.docs == before


async def test_superseded_without_superseded_by_is_refused_and_writes_nothing(store):
    fid = await create(store)
    before = snapshot(store)

    res = await store.adapter.update_fact(fid, {"state": "SUPERSEDED"}, ACC)

    assert res["status"] == "failed" and "superseded_by" in res["message"]
    assert store.docs == before


async def test_superseded_with_superseded_by_closes_in_place_with_the_resolved_successor(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    b2 = (await store.adapter.update_fact(b, {"content": "likes green tea daily"}, ACC))["fact_id"]

    res = await store.adapter.update_fact(a, {"state": "SUPERSEDED", "superseded_by": b}, ACC)

    assert res["status"] == "updated" and res["fact_id"] == a and res["version"] == 1
    closed = store.docs[a]
    assert (closed["state"], closed["is_current"], closed["superseded_by"]) == ("superseded", False, b2)
    assert closed["valid_to"] is not None and len(store.docs) == 3
    assert store.docs[b2]["state"] == "current"


@pytest.mark.parametrize("which", ["foreign", "missing"])
async def test_superseded_by_a_foreign_or_missing_successor_is_refused_and_names_nothing(store, which):
    a = await create(store, "likes tea")
    foreign = await create(store, "foreign fact", account_id=OTHER, user_id="usr-2")
    successor = foreign if which == "foreign" else "missing-successor-id"
    before = snapshot(store)

    res = await store.adapter.update_fact(a, {"state": "SUPERSEDED", "superseded_by": successor}, ACC)

    assert res["status"] == "failed" and "not found" in res["message"]
    assert successor not in str(res) and foreign not in str(res)
    assert store.docs == before


@pytest.mark.parametrize("closing", ["archived", "merged"])
async def test_a_successor_that_is_not_current_is_named_as_the_successor(store, closing):
    a = await create(store, "likes tea")
    s = await create(store, "likes green tea")
    if closing == "archived":
        await store.adapter.update_fact(s, {"state": "ARCHIVED"}, ACC)
    else:
        merged = (await store.adapter.merge_facts([s], "likes green tea daily", meta()))["new_fact_id"]
    before = snapshot(store)

    res = await store.adapter.update_fact(a, {"state": "SUPERSEDED", "superseded_by": s}, ACC)

    assert res["status"] == "failed" and res["fact_id"] == a
    assert res["message"].startswith(f"successor fact {s} ")
    assert res["message"].endswith("; search again") and res["message"].count("search again") == 1
    assert ("is archived" in res["message"]) if closing == "archived" else (merged in res["message"])
    assert store.docs == before


async def test_superseded_by_the_fact_itself_or_its_own_older_id_is_refused(store):
    a = await create(store, "likes tea")
    a2 = (await store.adapter.update_fact(a, {"content": "likes black tea"}, ACC))["fact_id"]
    before = snapshot(store)

    for successor in (a, a2):
        res = await store.adapter.update_fact(a2, {"state": "SUPERSEDED", "superseded_by": successor}, ACC)
        assert res["status"] == "failed" and "different fact" in res["message"]
    assert store.docs == before


async def test_update_of_a_missing_id_is_not_found_before_its_values_are_parsed(store):
    res = await store.adapter.update_fact("missing", {"state": "FOO"}, ACC)

    assert res == {"fact_id": "missing", "status": "not_found", "message": "Fact not found"}


# ---------------------------------------------------------------------------------------------------- conflicts

async def test_precondition_conflict_reresolves_once_and_succeeds(store):
    fid = await create(store)
    embedder = SlowEmbedder(store.embedder, 0)
    store.adapter._embedding = embedder
    writes = WriteOutcomes(store.repo, "write_superseding")
    resolutions = count_resolutions(store.repo)
    store.interfere(fid, {"reported_date": STALE_DATE})    # interleaving source: a commit lands at the transactional read

    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC)

    assert res["status"] == "updated" and res["version"] == 2
    assert store.docs[fid]["reported_date"] == STALE_DATE and store.docs[fid]["superseded_by"] == res["fact_id"]
    assert writes.outcomes == ["conflict", "ok"] and resolutions == [fid, fid]
    assert embedder.calls == 1       # the texts did not change between the attempts


async def test_second_conflict_is_a_failed_result_and_writes_nothing(store):
    fid = await create(store)
    writes = WriteOutcomes(store.repo, "write_superseding")
    store.interfere(fid, {"reported_date": STALE_DATE})
    store.interfere(fid, {"context": "second interference"})

    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC)

    assert res["status"] == "failed" and res["fact_id"] == fid and "changed concurrently" in res["message"]
    assert writes.outcomes == ["conflict", "conflict"]
    assert list(store.docs) == [fid]
    doc = store.docs[fid]
    assert doc["state"] == "current" and doc["superseded_by"] is None and doc["text"] == "mother is Valentina"
    assert doc["reported_date"] == STALE_DATE and doc["context"] == "second interference"


async def test_in_place_write_conflict_reresolves_once_too(store):
    fid = await create(store)
    writes = WriteOutcomes(store.repo, "update_fact_fields")
    store.interfere(fid, {"reported_date": STALE_DATE})

    res = await store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC)

    assert res["status"] == "updated" and res["fact_id"] == fid
    assert writes.outcomes == ["conflict", "ok"] and store.docs[fid]["state"] == "archived"
    assert store.docs[fid]["reported_date"] == STALE_DATE


async def test_gathered_content_and_archive_on_one_fact_archive_wins_content_fails_one_is_current_per_lineage(store):
    # Reviewer edit (F1 follow-up): the race was a 20 ms sleep in the content call's embedder against the archive's path.
    # Interleaving source now: the content call's embedder waits on an event set by the archive's in-place commit.
    fid = await create(store)
    archived = asyncio.Event()
    embedder = GatedEmbedder(store.embedder, gate=archived)
    store.adapter._embedding = embedder
    writes = WriteOutcomes(store.repo, "write_superseding")
    in_place = WriteOutcomes(store.repo, "update_fact_fields", on_ok=archived)

    content, archive = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC),
        store.adapter.update_fact(fid, {"state": "ARCHIVED"}, ACC),
    )

    assert archive["status"] == "updated" and archive["fact_id"] == fid
    assert content["status"] == "failed" and "is archived" in content["message"]
    assert embedder.calls == 1 and writes.outcomes == ["conflict"]    # the content call embedded, then lost the race
    assert in_place.outcomes == ["ok"]                                  # the archive committed first, once
    assert list(store.docs) == [fid] and store.docs[fid]["state"] == "archived"
    assert store.docs[fid]["text"] == "mother is Valentina"
    lineage = store.docs[fid]["lineage_id"]
    assert [d for d in store.docs.values() if d["lineage_id"] == lineage and d["is_current"]] == [store.docs[fid]]


async def test_gathered_content_updates_the_later_one_is_refused_when_a_version_landed_between_attempts(store):
    fid = await create(store)
    embedder = SlowEmbedder(store.embedder, 0.02)       # interleaving source: slow embedder on the adapter
    store.adapter._embedding = embedder
    writes = WriteOutcomes(store.repo, "write_superseding")

    first, second = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC),
        store.adapter.update_fact(fid, {"content": "mother is Valya"}, ACC),
    )

    assert (first["status"], first["version"]) == ("updated", 2)
    assert second == {"fact_id": fid, "status": "failed", "message": f"fact {fid} changed concurrently; search again"}
    assert writes.outcomes == ["ok", "conflict"] and embedder.calls == 2    # both embedded, the second lost the race
    assert sorted(store.docs) == sorted([fid, first["fact_id"]])
    assert store.docs[first["fact_id"]]["text"] == "mother is Valentyna"
    assert [d["id"] for d in currents(store, store.docs[fid]["lineage_id"])] == [first["fact_id"]]


async def test_gathered_tags_updates_the_later_one_is_refused_too(store):
    fid = await create(store)
    store.adapter._embedding = SlowEmbedder(store.embedder, 0.02)
    writes = WriteOutcomes(store.repo, "write_superseding")    # reviewer edit (F1 follow-up): the S-2 branch, not a 2nd conflict

    first, second = await asyncio.gather(
        store.adapter.update_fact(fid, {"tags": ["relatives"]}, ACC),
        store.adapter.update_fact(fid, {"tags": ["kin"]}, ACC),
    )

    assert first["status"] == "updated" and second["status"] == "failed" and "changed concurrently" in second["message"]
    assert store.docs[first["fact_id"]]["tags"] == ["relatives", "consolidated"] and len(store.docs) == 2
    assert writes.outcomes == ["ok", "conflict"]


async def test_a_metadata_update_still_reresolves_onto_a_version_that_landed_between_attempts(store):
    fid = await create(store)
    store.adapter._embedding = SlowEmbedder(store.embedder, 0.02)

    content, metadata = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC),
        store.adapter.update_fact(fid, {"metadata": {"city": "Kyiv"}}, ACC),
    )

    assert content["status"] == "updated" and metadata["status"] == "updated" and metadata["version"] == 3
    head = store.docs[metadata["fact_id"]]
    assert head["text"] == "mother is Valentyna" and head["metadata"] == {"name": "Valentina", "city": "Kyiv"}
    assert head["replaces_fact_id"] == content["fact_id"]


class _EveryTokenEqual:
    def __eq__(self, other):
        return True

    def __ne__(self, other):
        return False

    __hash__ = None


class _UncheckedRepo(FirestoreFactRepository):
    """The repository with the precondition comparison disabled: every token it hands out equals every snapshot's."""

    async def resolve_current(self, fact_id, account_id, accepted):
        stored = await super().resolve_current(fact_id, account_id, accepted)
        return None if stored is None else StoredFact(stored.fact, _EveryTokenEqual())


async def test_negative_control_without_token_check_forks_the_lineage(store):
    fid = await create(store)
    store.adapter._repo = _UncheckedRepo(store.db, make_env_config(), embedding_service=store.embedder)
    store.adapter._embedding = SlowEmbedder(store.embedder, 0.02)

    first, second = await asyncio.gather(
        store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC),
        store.adapter.update_fact(fid, {"content": "mother is Valya"}, ACC),
    )

    assert first["version"] == second["version"] == 2 and first["fact_id"] != second["fact_id"]
    forked = currents(store, store.docs[fid]["lineage_id"])
    assert sorted(d["id"] for d in forked) == sorted([first["fact_id"], second["fact_id"]])    # two current heads


# ------------------------------------------------------------------------------------------------------- tags

@pytest.mark.parametrize("tags", [None, [], "family", ["family", 3]])
async def test_tags_null_and_empty_refused_and_system_tags_kept(store, tags):
    fid = await create(store)
    before = snapshot(store)

    refused = await store.adapter.update_fact(fid, {"tags": tags}, ACC)
    assert refused["status"] == "failed" and "tags" in refused["message"] and store.docs == before

    res = await store.adapter.update_fact(fid, {"tags": ["relatives"]}, ACC)
    assert store.docs[res["fact_id"]]["tags"] == ["relatives", "consolidated"]


async def test_anchor_tag_of_a_principle_is_kept_in_stored_order(store):
    fid = await create(store, "never rush", type="principle")
    assert store.docs[fid]["tags"] == ["family", "mother", "consolidated", "anchor"]

    res = await store.adapter.update_fact(fid, {"tags": ["rules"]}, ACC)

    assert store.docs[res["fact_id"]]["tags"] == ["rules", "consolidated", "anchor"]


async def test_resending_the_stored_tags_without_system_tags_changes_nothing_but_last_updated(store):
    fid = await create(store)

    res = await store.adapter.update_fact(fid, {"tags": ["family", "mother"]}, ACC)

    assert res["fact_id"] == fid and res["version"] == 1 and list(store.docs) == [fid]


# ----------------------------------------------------------------------------------------- embedding and values

async def test_update_with_an_embedder_that_returns_no_vector_fails_and_writes_nothing(store):
    fid = await create(store)
    before = snapshot(store)
    store.adapter._embedding = FixedEmbedder([embed("a"), None, embed("c")])

    res = await store.adapter.update_fact(fid, {"content": "mother is Valentyna"}, ACC)

    assert res["status"] == "failed" and "no vector" in res["message"] and store.docs == before


async def test_update_with_an_embedding_failure_fails_and_writes_nothing(store):
    fid = await create(store)
    before = snapshot(store)
    store.adapter._embedding = FailingEmbedder()

    res = await store.adapter.update_fact(fid, {"content": "changed", "state": "ARCHIVED"}, ACC)

    assert res["status"] == "failed" and res["fact_id"] == fid and "embedding service down" in res["message"]
    assert store.docs == before


@pytest.mark.parametrize("bad", [{"content": ""}, {"content": "   "}, {"state": "FOO"}, {"reported_date": "Oct 2026"}])
async def test_a_bad_value_fails_before_embedding_and_writes_nothing(store, bad):
    fid = await create(store)
    before = snapshot(store)
    embedder = SlowEmbedder(store.embedder, 0)
    store.adapter._embedding = embedder

    res = await store.adapter.update_fact(fid, {"temporal_class": "dynamic", **bad}, ACC)

    assert res["status"] == "failed" and embedder.calls == 0 and store.docs == before


# ------------------------------------------------------------------------------------------------------- merge

async def test_merge_with_a_bad_value_writes_nothing_and_leaves_sources_current(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    before = snapshot(store)
    embedder = SlowEmbedder(store.embedder, 0)
    store.write._embedding = embedder
    resolutions = count_resolutions(store.repo)

    for bad in ({"domain": "nonsense"}, {"state": "superseded"}, {"reported_date": "Oct 2026"}):
        res = await store.adapter.merge_facts([a, b], "likes tea daily", meta(**bad))
        assert res["status"] == "failed" and res["new_fact_id"] is None and res["old_fact_ids"] == []

    assert embedder.calls == 0 and resolutions == [] and store.docs == before


async def test_merge_embedding_failure_writes_nothing(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    before = snapshot(store)
    store.write._embedding = FailingEmbedder()

    res = await store.adapter.merge_facts([a, b], "likes tea daily", meta())

    assert res["status"] == "failed" and res["new_fact_id"] is None and "embedding service down" in res["message"]
    assert store.docs == before


async def test_merge_sets_superseded_by_on_every_source_in_one_commit(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")

    res = await store.adapter.merge_facts([a, b], "likes tea daily", meta())

    merged = res["new_fact_id"]
    assert res["status"] == "merged" and res["old_fact_ids"] == [a, b]
    for source in (a, b):
        doc = store.docs[source]
        assert (doc["state"], doc["is_current"], doc["superseded_by"]) == ("superseded", False, merged)
        assert doc["valid_to"] is not None
        assert stamp(store, source) == stamp(store, merged)
    assert store.docs[merged]["superseded_by"] is None and store.docs[merged]["replaces_fact_id"] is None


async def test_merge_resolves_a_versioned_source_forward(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    a2 = (await store.adapter.update_fact(a, {"content": "likes black tea"}, ACC))["fact_id"]

    res = await store.adapter.merge_facts([a, b, a2], "likes tea daily", meta())

    assert res["status"] == "merged" and res["old_fact_ids"] == [a2, b]
    assert store.docs[a2]["superseded_by"] == res["new_fact_id"] == store.docs[b]["superseded_by"]
    assert store.docs[a]["superseded_by"] == a2


async def test_merge_of_an_already_merged_source_fails(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    first = (await store.adapter.merge_facts([a], "likes tea daily", meta()))["new_fact_id"]
    before = snapshot(store)

    res = await store.adapter.merge_facts([b, a], "likes tea again", meta())

    assert res["status"] == "failed" and res["old_fact_ids"] == [] and res["new_fact_id"] is None
    assert first in res["message"] and "merged" in res["message"]
    assert store.docs == before


async def test_merge_skips_a_foreign_merged_source(store):
    own = await create(store, "likes tea")
    foreign = await create(store, "foreign fact", account_id=OTHER, user_id="usr-2")
    await store.adapter.merge_facts([foreign], "foreign merged", meta(account_id=OTHER, user_id="usr-2"))

    res = await store.adapter.merge_facts([foreign, own], "likes tea daily", meta())

    assert res["status"] == "merged" and res["old_fact_ids"] == [own]
    assert store.docs[foreign]["superseded_by"] is not None and store.docs[own]["superseded_by"] == res["new_fact_id"]


async def test_merge_collapses_sources_that_resolve_to_one_document(store):
    a = await create(store, "likes tea")
    a2 = (await store.adapter.update_fact(a, {"content": "likes black tea"}, ACC))["fact_id"]

    res = await store.adapter.merge_facts([a, a2, a], "likes tea daily", meta())

    assert res["status"] == "merged" and res["old_fact_ids"] == [a2]


async def test_merge_conflict_with_unchanged_sources_reresolves_once(store):
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    embedder = SlowEmbedder(store.embedder, 0)
    store.write._embedding = embedder
    writes = WriteOutcomes(store.repo, "write_superseding")
    resolutions = count_resolutions(store.repo)
    store.interfere(b, {"reported_date": STALE_DATE})     # a reported_date-only write lands on a source at the read

    res = await store.adapter.merge_facts([a, b], "likes tea daily", meta())

    assert res["status"] == "merged" and res["old_fact_ids"] == [a, b]
    assert store.docs[b]["reported_date"] == STALE_DATE
    assert writes.outcomes == ["conflict", "ok"] and resolutions == [a, b, a, b]
    assert embedder.calls == 1                             # the merged fact is built once


async def test_merge_second_conflict_is_a_failed_result_and_writes_nothing(store):
    a = await create(store, "likes tea")
    writes = WriteOutcomes(store.repo, "write_superseding")
    store.interfere(a, {"reported_date": STALE_DATE})
    store.interfere(a, {"context": "second interference"})

    res = await store.adapter.merge_facts([a], "likes tea daily", meta())

    assert res["status"] == "failed" and res["old_fact_ids"] == [] and "changed concurrently" in res["message"]
    assert writes.outcomes == ["conflict", "conflict"] and list(store.docs) == [a]
    assert store.docs[a]["state"] == "current" and store.docs[a]["context"] == "second interference"


async def test_merge_fails_when_a_source_moved_forward_between_attempts(store):
    # Reviewer edit (F1 follow-up): the race was two sleep margins (10 ms vs 100 ms). Interleaving source now: gated
    # embedders on both sides. The update waits until the merge has resolved its sources (the merge's embedder signals);
    # the merge's embedder waits until the update's version has committed. So: merge reads a, update commits a2, merge
    # writes against a's old token and conflicts, re-resolves to [a2, b] and refuses.
    a = await create(store, "likes tea")
    b = await create(store, "likes green tea")
    merge_resolved, update_committed = asyncio.Event(), asyncio.Event()
    update_embedder = GatedEmbedder(store.embedder, gate=merge_resolved)
    merge_embedder = GatedEmbedder(store.embedder, gate=update_committed, signal=merge_resolved)
    store.adapter._embedding = update_embedder
    store.write._embedding = merge_embedder
    writes = WriteOutcomes(store.repo, "write_superseding", on_ok=update_committed)

    update, merge = await asyncio.gather(
        store.adapter.update_fact(a, {"content": "likes black tea"}, ACC),
        store.adapter.merge_facts([a, b], "likes tea daily", meta()),
    )

    assert update["status"] == "updated"
    assert merge["status"] == "failed" and merge["message"] == "source changed; search again"
    assert merge["new_fact_id"] is None and merge["old_fact_ids"] == []
    assert writes.outcomes == ["ok", "conflict"] and update_embedder.calls == 1 and merge_embedder.calls == 1
    assert len(store.docs) == 3 and store.docs[b]["state"] == "current"
    assert store.docs[a]["superseded_by"] == update["fact_id"] and store.docs[update["fact_id"]]["state"] == "current"


async def test_merge_without_an_eligible_source_fails_with_the_old_message_and_does_not_embed(store):
    embedder = SlowEmbedder(store.embedder, 0)
    store.write._embedding = embedder

    res = await store.adapter.merge_facts(["missing"], "merged", meta())

    assert res == {"new_fact_id": None, "old_fact_ids": [], "old_facts_state": "FAILED", "status": "failed",
                   "message": "Failed to supersede any facts"}
    assert embedder.calls == 0 and store.docs == {}


async def test_merged_fact_entity_is_built_from_the_given_attributes(store):
    a = await create(store, "likes tea")

    res = await store.adapter.merge_facts([a], "likes tea daily", meta(tags=["drinks"], metadata={"k": "v"}))

    new = FactEntity(**{k: v for k, v in store.docs[res["new_fact_id"]].items()
                        if k in FactEntity.model_fields and k not in VECTORS})
    assert (new.text, new.tags, new.metadata, new.version) == ("likes tea daily", ["drinks", "consolidated"], {"k": "v"}, 1)
    assert new.lineage_id != store.docs[a]["lineage_id"] and new.created_by_user_id == USER


async def test_completing_a_plan_without_a_version_document_raises(store):
    plan = FactUpdatePlan("in_place", {"last_updated": STALE_DATE}, None, False, None)

    with pytest.raises(RuntimeError, match="in_place plan carries no version document"):
        await store.adapter._complete_version(plan, None)
