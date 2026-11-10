"""FactSearchService (fact profile RFC §5): the query's embedding, the snapshot cache keyed by the baseline token, the two
kNN lists, the vector cache, the duplicate pass, the lazy place index and the log line, on `FactStore` (real repository
over the evaluating Firestore fake, index refusal ON)."""
import asyncio
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.entities import FactState
from src.services.fact_search_service import FactSearchService
from tests.support.fact_kit import ACC, OTHER, make_fact
from tests.support.fact_search_kit import query
from tests.support.fact_store import INDEXES_FILE, FactStore, HashEmbedder, embed
from tests.support.person_card_fakes import HeldCards

T1 = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
ROME = PlaceRow(41.9, 12.5, "Rome", "Roma", "Italy", 2_800_000, country_names=("Italia", "Italy"), geonameid="100")
PROFILE = dict(label_words=["x"], time_role=None, primary_domain="transport", domain_keys=["transport"],
               kinds=["document"], sensitivity="ordinary", place_keys=["pl:100", "co:italy"])


class RecordingRepo:
    """The real repository, noting which port calls the service made and with what."""

    def __init__(self, repo):
        self._repo = repo
        self.calls: List[tuple] = []

    def __getattr__(self, name):
        target = getattr(self._repo, name)
        if name not in ("list_search_snapshot", "nearest_fact_ids", "get_fact_vectors", "get_baseline_refreshed_at"):
            return target

        async def call(*args: Any):
            self.calls.append((name, *args))
            return await target(*args)

        return call

    def named(self, name):
        return [c[1:] for c in self.calls if c[0] == name]


class RecordingEmbedder(HashEmbedder):
    def __init__(self):
        self.batches: List[tuple] = []
        self.fail = False

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.batches.append((tuple(texts), task_type))
        if self.fail:
            raise RuntimeError("embedding down")
        return await super().get_embeddings_batch(texts, task_type)


class World:
    def __init__(self):
        self.store = FactStore(indexes=INDEXES_FILE)
        self.repo = RecordingRepo(self.store.repo)
        self.embedder = RecordingEmbedder()
        self.place_loads = 0
        self.service = FactSearchService(self.repo, self.embedder, self._places, HeldCards())

    def _places(self):
        self.place_loads += 1
        return PlaceNames([ROME])

    async def seed(self, text, **over):
        fact = make_fact(text, **{**PROFILE, **over})
        await self.store.repo.add_fact(fact)
        return fact

    async def baseline(self, when=T1, account=ACC):
        await self.store.db.collection("eval_user_context").document(account).set({"refreshed_at": when})

    async def search(self, **kw):
        return [f.id for f in await self.service.search(ACC, query(**kw))]


@pytest.fixture
def world():
    return World()


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[FactSearch]")]


# ------------------------------------------------------------------------------------------------ the search

async def test_a_subject_finds_the_fact_nearest_to_it(world):
    for text in ("alpha", "beta", "gamma"):
        await world.seed(text)
    await world.baseline()
    assert (await world.search(subject="gamma"))[0] == "f-gamma"


async def test_a_kind_finds_the_fact_whose_label_vector_is_nearest(world):
    await world.seed("car", label_vector=embed("vehicle cover"))
    await world.seed("trip", label_vector=embed("holiday"))
    await world.baseline()
    assert (await world.search(kind="holiday"))[0] == "f-trip"


async def test_only_the_current_facts_of_the_account_are_returned(world):
    await world.seed("mine")
    await world.seed("stale", state=FactState.STALE)
    await world.seed("theirs", account=OTHER)
    await world.baseline()
    assert await world.search(subject="theirs", domains=["transport"]) == ["f-mine"]


async def test_the_result_is_the_snapshot_entities_with_their_profile_and_no_vectors(world):
    await world.seed("car")
    await world.baseline()
    (fact,) = await world.service.search(ACC, query(subject="car"))
    assert fact.id == "f-car" and fact.domain_keys == ["transport"] and fact.vector is None


async def test_the_key_lists_and_the_knn_lists_give_candidates_and_the_score_orders_them(world, caplog):
    await world.seed("alpha", domain_keys=["money"], primary_domain="money")
    await world.seed("beta")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        got = await world.search(subject="beta", domains=["money"])
    # alpha is in both lists, beta only in the semantic one: no vote by list count, the subject's own fact leads
    assert got == ["f-beta", "f-alpha"]
    assert "lists=semantic:2,domain:1 cands=2 " in lines(caplog)[-1]


# ------------------------------------------------------------------------------------------------ embedding and kNN

async def test_the_query_text_is_embedded_once_as_a_query(world):
    await world.seed("alpha")
    await world.baseline()
    await world.search(subject="alpha", kind="holiday")
    assert world.embedder.batches == [(("alpha", "holiday"), "RETRIEVAL_QUERY")]
    await world.search(subject="alpha")
    assert world.embedder.batches[-1] == (("alpha",), "RETRIEVAL_QUERY")
    await world.search(kind="holiday")
    assert world.embedder.batches[-1] == (("holiday",), "RETRIEVAL_QUERY")


async def test_a_query_without_subject_or_kind_embeds_nothing_and_runs_no_knn(world):
    await world.seed("alpha")
    await world.baseline()
    assert await world.search(domains=["transport"]) == ["f-alpha"]
    assert world.embedder.batches == [] and world.repo.named("nearest_fact_ids") == []


async def test_knn_asks_for_60_in_the_text_and_label_fields_of_the_account(world):
    await world.seed("alpha")
    await world.baseline()
    await world.search(subject="alpha", kind="holiday")
    assert sorted((c[1], c[3]) for c in world.repo.named("nearest_fact_ids")) == [("label_vector", 60), ("vector", 60)]
    assert {c[0] for c in world.repo.named("nearest_fact_ids")} == {ACC}
    await world.search(subject="alpha")
    assert [c[1] for c in world.repo.named("nearest_fact_ids")[2:]] == ["vector"]


async def test_the_semantic_list_is_cut_at_60_and_the_result_at_30(world, caplog):
    for n in range(65):
        await world.seed(f"fact {n}")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        got = await world.search(subject="fact 3")
    # The kNN list is cut at its depth; the log shows the depth over the 65 facts that have a vector.
    assert "lists=semantic:60/65 cands=60 " in lines(caplog)[-1]
    assert len(got) == 30 and "kept=30 " in lines(caplog)[-1]


# ------------------------------------------------------------------------------------------------ snapshot cache

async def test_the_snapshot_is_reused_while_the_baseline_token_stays(world, caplog):
    await world.seed("alpha")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        await world.search(subject="alpha")
        await world.search(subject="alpha")
    assert len(world.repo.named("list_search_snapshot")) == 1
    assert [re.search(r"snapshot=(\S+)", m).group(1) for m in lines(caplog)] == ["load:1", "hit"]


async def test_the_snapshot_is_reloaded_when_the_token_changes(world, caplog):
    await world.seed("alpha")
    await world.baseline()
    await world.search(subject="alpha")
    await world.seed("beta")
    await world.baseline(T1 + timedelta(hours=1))
    with caplog.at_level(logging.INFO):
        assert sorted(await world.search(domains=["transport"])) == ["f-alpha", "f-beta"]
    assert len(world.repo.named("list_search_snapshot")) == 2 and "snapshot=load:2" in lines(caplog)[-1]


async def test_without_a_token_the_snapshot_is_never_cached(world):
    await world.seed("alpha")
    await world.search(subject="alpha")
    await world.seed("beta")
    assert sorted(await world.search(domains=["transport"])) == ["f-alpha", "f-beta"]
    assert len(world.repo.named("list_search_snapshot")) == 2


async def test_the_cache_is_per_account(world):
    await world.seed("alpha")
    await world.seed("other", account=OTHER)
    await world.baseline()
    await world.baseline(account=OTHER)
    await world.search(subject="alpha")
    await world.service.search(OTHER, query(subject="other"))
    assert len(world.repo.named("list_search_snapshot")) == 2


async def test_concurrent_searches_load_the_snapshot_once(world):
    await world.seed("alpha")
    await world.baseline()
    await asyncio.gather(*(world.search(subject="alpha") for _ in range(4)))
    assert len(world.repo.named("list_search_snapshot")) == 1


async def test_a_fact_the_snapshot_does_not_know_is_dropped_from_the_knn_lists_and_counted(world, caplog):
    await world.seed("alpha")
    await world.baseline()
    await world.search(subject="alpha")
    await world.seed("newcomer")                    # written after the snapshot, token unchanged
    with caplog.at_level(logging.INFO):
        assert await world.search(subject="newcomer") == ["f-alpha"]
    assert "unsnapped=1" in lines(caplog)[-1] and "snapshot=hit" in lines(caplog)[-1]


# ------------------------------------------------------------------------------------------------ vectors

async def test_the_first_load_fetches_every_vector_and_a_reload_only_the_new_facts(world):
    await world.seed("alpha")
    await world.seed("beta")
    await world.baseline()
    await world.search(subject="alpha")
    await world.seed("gamma")
    await world.baseline(T1 + timedelta(hours=1))
    await world.search(subject="alpha")
    # The text-vector reads only (no field, or "vector"); label vectors are read whole on each load (scored tests).
    fetched = [sorted(c[1]) for c in world.repo.named("get_fact_vectors") if c[2:] in ((), ("vector",))]
    assert fetched == [["f-alpha", "f-beta"], ["f-gamma"]]


async def test_the_vector_cache_drops_the_facts_that_left_the_snapshot(world):
    await world.seed("alpha")
    gone = await world.seed("beta")
    await world.baseline()
    await world.search(subject="alpha")
    world.store.docs[gone.id]["state"] = "stale"
    await world.baseline(T1 + timedelta(hours=1))
    await world.search(subject="alpha")
    assert set(world.service._known_vectors[ACC]) == {"f-alpha"}


async def test_a_fact_without_a_text_vector_has_no_similarity_and_is_never_a_duplicate(world):
    await world.seed("twin", id="f-1")
    await world.seed("twin", id="f-2", vector=None)
    await world.baseline()
    assert sorted(await world.search(domains=["transport"])) == ["f-1", "f-2"]


# ------------------------------------------------------------------------------------------------ duplicates

async def test_a_near_duplicate_is_removed_after_fusion(world):
    await world.seed("same words", id="f-1")
    await world.seed("same words", id="f-2")
    await world.seed("other words")
    await world.baseline()
    got = await world.search(subject="same words")
    assert len(got) == 2 and got[0] in ("f-1", "f-2") and "f-other words" in got


# ------------------------------------------------------------------------------------------------ places

async def test_the_place_index_loads_on_the_first_query_with_a_place_and_once(world):
    await world.seed("trip", place_keys=["pl:100", "co:italy"])
    await world.seed("home", place_keys=["pl:7"])
    await world.baseline()
    assert await world.search(subject="home", domains=["transport"]) and world.place_loads == 0
    assert (await world.search(places=[("Italy", "")], domains=["nothing"]))[0] == "f-trip"
    await world.search(places=[("Rome", "")])
    assert world.place_loads == 1


async def test_an_unknown_place_adds_no_list(world, caplog):
    await world.seed("trip")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        await world.search(places=[("Atlantis", "")], subject="trip")
    assert "places" not in lines(caplog)[-1]


# ------------------------------------------------------------------------------------------------ log and errors

async def test_one_line_per_search_names_the_path_the_lists_and_the_counts(world, caplog):
    await world.seed("alpha")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        await world.search(subject="alpha", domains=["transport"])
    (line,) = lines(caplog)
    assert re.fullmatch(r"\[FactSearch\] path=test acc=acc-1 phrases=1 lists=semantic:1,domain:1 cands=1 "
                        r"gate_dropped=0 kept=1 unsnapped=0 snapshot=load:1 ms=\d+", line)


async def test_the_gate_count_is_logged(world, caplog):
    await world.seed("secret", sensitivity="intimate")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        assert await world.search(subject="secret") == []
    assert "gate_dropped=1" in lines(caplog)[-1]


async def test_an_embedding_failure_is_logged_and_raised(world, caplog):
    world.embedder.fail = True
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="embedding down"):
        await world.search(subject="alpha")
    assert any("[FactSearch]" in r.getMessage() and "embedding down" in r.getMessage() for r in caplog.records)


async def test_a_store_failure_is_logged_and_raised(world, caplog):
    world.store.db.collection("eval_domain_facts").fail = RuntimeError("firestore down")
    await world.baseline()
    with caplog.at_level(logging.ERROR), pytest.raises(RuntimeError, match="firestore down"):
        await world.search(domains=["transport"])
    assert any("firestore down" in r.getMessage() for r in caplog.records if r.levelno == logging.ERROR)
