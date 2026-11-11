"""FactSearchService, scored search (fact profile RFC §5.2, §5.3, OD-14): one embedding batch for every query text, the
message list over the loaded text vectors, label vectors and people cards read with each snapshot load, the owner's home
resolved once per location, `exclude`, several phrases fused, and the store's vector read by field. Runs on `FactStore`
(real repository over the evaluating Firestore fake, index refusal ON); examples from the invented universe."""
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from src.domain.fact_search_lists import DEPTH_KNN
from src.domain.fact_search_snapshot import name_skeleton
from src.domain.person_cards import PersonCard
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.services import fact_search_service as service_module
from src.services.fact_search_service import FactSearchService
from tests.support.fact_kit import ACC, make_fact
from tests.support.fact_search_kit import query
from tests.support.fact_store import INDEXES_FILE, FactStore, HashEmbedder, embed
from tests.support.person_card_fakes import HeldCards

T1 = datetime(2026, 11, 9, 8, 0, tzinfo=timezone.utc)
PT = ("Portugal",)
SHELBYVILLE = PlaceRow(41.2, -8.5, "Shelbyville", "Shelbyville", "Portugal", 150_000, country_names=PT,
                       geonameid="9100002")
SPRINGFIELD = PlaceRow(41.25, -8.55, "Springfield", "Springfield", "Portugal", 20_000, country_names=PT,
                       large=SHELBYVILLE, geonameid="9100001")
COIMBRA = PlaceRow(40.2, -8.42, "Coimbra", "Coimbra", "Portugal", 140_000, country_names=PT, geonameid="9100004")
HOME = "Springfield, Portugal"
PROFILE = dict(label_words=["x"], time_role=None, primary_domain="family", domain_keys=["family"], kinds=[],
               sensitivity="ordinary", place_keys=[])
MARIA = PersonCard("doe_mari", "Maria Doe", ("Maria Doe", "Mia"),
                   tuple("x:" + k for k in sorted(name_skeleton("Maria Doe") | name_skeleton("Mia"))), "mother", 12)


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

    async def get_embeddings_batch(self, texts, task_type="RETRIEVAL_DOCUMENT"):
        self.batches.append((tuple(texts), task_type))
        return await super().get_embeddings_batch(texts, task_type)


class World:
    def __init__(self, *cards: PersonCard):
        self.store = FactStore(indexes=INDEXES_FILE)
        self.repo = RecordingRepo(self.store.repo)
        self.embedder = RecordingEmbedder()
        self.cards = HeldCards(*cards)
        self.service = FactSearchService(self.repo, self.embedder, lambda: PlaceNames([SPRINGFIELD, SHELBYVILLE, COIMBRA]),
                                         self.cards)

    async def seed(self, text, **over):
        fact = make_fact(text, **{**PROFILE, **over})
        await self.store.repo.add_fact(fact)
        return fact

    async def baseline(self, when=T1):
        await self.store.db.collection("eval_user_context").document(ACC).set({"refreshed_at": when})

    async def search(self, q, **kw):
        return [f.id for f in await self.service.search(ACC, q, **kw)]

    async def phrases(self, *queries):
        return [f.id for f in (await self.service.search_phrases(ACC, list(queries))).facts]


@pytest.fixture
def world():
    return World()


def lines(caplog):
    return [r.getMessage() for r in caplog.records if r.getMessage().startswith("[FactSearch]")]


def with_message(q, message):
    return replace(q, message=message)


# ------------------------------------------------------------------------------------------------ embedding and kNN

async def test_every_query_text_of_every_phrase_goes_in_one_embedding_batch(world):
    await world.seed("Knee surgery 2026-10-05")
    await world.baseline()
    first = with_message(query(subject="knee surgery", kind="surgery"), "how did my knee surgery go")
    second = with_message(query(subject="dentist check-up"), "how did my knee surgery go")
    await world.phrases(first, second)
    assert world.embedder.batches == [
        (("knee surgery", "surgery", "how did my knee surgery go", "dentist check-up"), "RETRIEVAL_QUERY")]


async def test_knn_asks_for_the_list_depth_in_both_fields_per_phrase(world):
    await world.seed("Knee surgery 2026-10-05")
    await world.baseline()
    await world.phrases(query(subject="knee", kind="surgery"), query(subject="dentist"))
    assert sorted((c[1], c[3]) for c in world.repo.named("nearest_fact_ids")) == [
        ("label_vector", DEPTH_KNN), ("vector", DEPTH_KNN), ("vector", DEPTH_KNN)]


async def test_the_message_list_comes_from_the_loaded_vectors_without_a_knn_call(world, caplog):
    await world.seed("Knee surgery 2026-10-05")
    await world.seed("Car service 2026-11-05")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        got = await world.search(with_message(query(), "Car service 2026-11-05"))
    assert got[0] == "f-Car service 2026-11-05"
    assert world.repo.named("nearest_fact_ids") == []
    assert "message:2" in lines(caplog)[-1]


# ------------------------------------------------------------------------------------------------ per load

async def test_label_vectors_are_read_for_every_snapshot_fact_on_each_load(world):
    await world.seed("Knee surgery 2026-10-05", label_vector=embed("surgery"))
    await world.seed("Dentist check-up 2026-10-12", label_vector=embed("check-up"))
    await world.baseline()
    await world.search(query(kind="check-up"))
    await world.search(query(kind="check-up"))              # snapshot hit: nothing read
    await world.seed("Car service 2026-11-05")
    await world.baseline(T1 + timedelta(hours=1))
    await world.search(query(kind="check-up"))
    label_reads = [sorted(c[1]) for c in world.repo.named("get_fact_vectors") if c[2:] == ("label_vector",)]
    assert label_reads == [
        ["f-Dentist check-up 2026-10-12", "f-Knee surgery 2026-10-05"],
        ["f-Car service 2026-11-05", "f-Dentist check-up 2026-10-12", "f-Knee surgery 2026-10-05"],
    ]


async def test_the_label_similarity_orders_by_the_kind(world):
    await world.seed("Knee surgery 2026-10-05", label_vector=embed("surgery"))
    await world.seed("Dentist check-up 2026-10-12", label_vector=embed("check-up"))
    await world.baseline()
    assert (await world.search(query(kind="check-up")))[0] == "f-Dentist check-up 2026-10-12"


async def test_people_cards_are_read_on_each_load_and_raise_another_spelling():
    for cards, first in (((MARIA,), "f-b Mia"), ((), "f-a Peter")):
        world = World(*cards)
        await world.seed("a Peter", people=[{"name": "Peter Doe", "role": "", "name_keys": [], "role_keys": []}])
        await world.seed("b Mia", people=[{"name": "Mia", "role": "", "name_keys": [], "role_keys": []}])
        await world.baseline()
        got = await world.search(query(people=["Maria Doe"], domains=["family"]))
        assert got[0] == first
        await world.search(query(people=["Maria Doe"], domains=["family"]))
        await world.baseline(T1 + timedelta(hours=1))
        await world.search(query(people=["Maria Doe"], domains=["family"]))
        assert world.cards.reads == [ACC, ACC]


# ------------------------------------------------------------------------------------------------ home

async def test_the_home_is_resolved_once_per_location_and_lifts_the_facts_at_home(world, monkeypatch, caplog):
    resolved = []
    real = service_module.resolve_home
    monkeypatch.setattr(service_module, "resolve_home", lambda names, loc: resolved.append(loc) or real(names, loc))
    await world.seed("a Hamburg conference Oct 15", place_keys=["pl:9100009"])
    await world.seed("b Christmas lunch at Maria's", place_keys=["pl:9100001", "co:portugal"])
    await world.baseline()
    with caplog.at_level(logging.INFO):
        got = await world.search(replace(query(domains=["family"]), home=HOME))
        await world.search(replace(query(domains=["family"]), home=HOME))
    assert got[0] == "f-b Christmas lunch at Maria's"
    assert resolved == [HOME]
    assert "home:1" in lines(caplog)[-1]
    assert await world.search(query(domains=["family"])) == ["f-a Hamburg conference Oct 15",
                                                             "f-b Christmas lunch at Maria's"]


# ------------------------------------------------------------------------------------------------ exclude and phrases

async def test_an_excluded_fact_is_never_returned(world):
    await world.seed("Knee surgery 2026-10-05")
    await world.seed("Dentist check-up 2026-10-12")
    await world.baseline()
    got = await world.search(query(domains=["family"]), exclude={"f-Knee surgery 2026-10-05"})
    assert got == ["f-Dentist check-up 2026-10-12"]


async def test_several_phrases_fuse_their_lists_and_log_one_line(world, caplog):
    await world.seed("Weekend in Coimbra with Clara Mar 6-8", domain_keys=["travel"], primary_domain="travel")
    await world.seed("Clara's birthday is on 07-14", domain_keys=["family"], primary_domain="family")
    await world.seed("Car service 2026-11-05", domain_keys=["vehicles"], primary_domain="vehicles")
    await world.baseline()
    with caplog.at_level(logging.INFO):
        got = await world.phrases(query(subject="Weekend in Coimbra with Clara Mar 6-8"),
                                  query(subject="Clara's birthday is on 07-14"))
    assert got[:2] == ["f-Weekend in Coimbra with Clara Mar 6-8", "f-Clara's birthday is on 07-14"]
    (line,) = lines(caplog)
    assert re.fullmatch(r"\[FactSearch\] path=test acc=acc-1 phrases=2 modes=top,top lists=semantic:3 cands=3 "
                        r"gate_dropped=0 kept=3 unsnapped=0 snapshot=load:3 ms=\d+", line)


async def test_one_phrase_is_the_plain_search(world):
    await world.seed("Knee surgery 2026-10-05")
    await world.seed("Dentist check-up 2026-10-12")
    await world.baseline()
    q = query(subject="Dentist check-up 2026-10-12")
    assert await world.phrases(q) == await world.search(q)


# ------------------------------------------------------------------------------------------------ the store

async def test_the_store_reads_the_label_vector_by_field_and_refuses_an_unknown_field(world):
    await world.seed("Knee surgery 2026-10-05", label_vector=embed("surgery"))
    got = await world.store.repo.get_fact_vectors(ACC, ["f-Knee surgery 2026-10-05"], "label_vector")
    assert list(got) == ["f-Knee surgery 2026-10-05"] and got["f-Knee surgery 2026-10-05"] == pytest.approx(
        embed("surgery"))
    with pytest.raises(ValueError):
        await world.store.repo.get_fact_vectors(ACC, ["f-Knee surgery 2026-10-05"], "tags_vector")
