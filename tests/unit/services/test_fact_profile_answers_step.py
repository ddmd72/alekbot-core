"""The owner's answers in the profile step (fact profile RFC §4.2, §4.7): an answer makes only the facts naming its point
`stale_derivation`, re-derived with no profiler call; an unreadable answers document ends the step; a called fact
stores the derivation of its new answer. The world of `test_fact_profile_service` with a fixture atlas; invented cast."""
import logging
from datetime import datetime, timezone
from typing import Optional

from src.domain.fact_profile_fingerprint import profile_derivation, registry_digest
from src.domain.place_lookup import PlaceRow
from src.domain.place_names import PlaceNames
from src.domain.profile_answers import (
    EMPTY_ANSWERS, OwnerAnswer, PlaceAnswer, ProfileAnswers, answers_digest, place_point_key,
)
from src.ports.profile_answer_repository import ProfileAnswerRepository
from tests.support.fact_kit import ACC, T0 as SEEDED_AT
from tests.unit.services.test_fact_profile_service import ATLAS_DIGEST, World, answer, fid, ok

AT = datetime(2026, 11, 10, 9, 0, tzinfo=timezone.utc)
NAMES = PlaceNames([
    PlaceRow(1.0, 1.0, "Rome", "Rome", "United States", 10, country_names=("United States",), geonameid="2000"),
    PlaceRow(2.0, 2.0, "Rome", "Roma", "Italy", 1000, country_names=("Italia", "Italy"), geonameid="1000"),
    PlaceRow(3.0, 3.0, "Porto", "Porto", "Portugal", 1000, country_names=("Portugal",), geonameid="3000"),
])
ALEX = "Alex has a trip to Rome"
HOTEL = "Alex books the Albergo Immaginario in Rome"
HANNA = "Hanna lives in Porto"
ROME = PlaceAnswer(key=place_point_key("Rome", ""), city="Rome", country="", resolution="atlas",
                     geonameid="2000", question_id="pq1", answered_at=AT)


class Answers(ProfileAnswerRepository):
    def __init__(self, fail: bool = False) -> None:
        self.answers, self.fail, self.reads = EMPTY_ANSWERS, fail, 0

    async def get(self, account_id: str) -> ProfileAnswers:
        self.reads += 1
        if self.fail:
            raise ValueError("the stored profile answers do not parse")
        return self.answers

    async def record(self, account_id: str, answer: OwnerAnswer) -> bool:
        if self.answers.has(answer.key):
            return False
        self.answers = self.answers.with_answer(answer)
        return True


def build(store: Optional[Answers]) -> World:
    world = World(profile_answers=store)

    async def atlas():
        world.atlas_calls += 1
        return NAMES, ATLAS_DIGEST

    world.service._atlas = atlas
    rome = ok(answer(places=[{"city": "Rome", "country": "", "source": "Rome"}]))
    world.profiler.replies.update({ALEX: rome, HOTEL: rome,
                                   HANNA: ok(answer(places=[{"city": "Porto", "country": "Portugal",
                                                             "source": "Porto"}]))})
    return world


def base_derivation() -> str:
    return profile_derivation(registry_digest(), ATLAS_DIGEST, False, SEEDED_AT.year)


async def test_an_answer_re_derives_only_the_facts_naming_it_without_a_call():
    store = Answers()
    world = build(store)
    for text in (ALEX, HOTEL, HANNA):
        await world.seed(text)
    await world.profile_everything()
    assert world.docs[fid(ALEX)]["places_unresolved"] == [{"city": "Rome", "country": ""}]
    porto_before = dict(world.docs[fid(HANNA)])
    world.profiler.calls.clear()

    await store.record(ACC, ROME)
    scan = await world.service.scan(ACC)
    run = await world.run()

    assert (scan.stale_derivation, scan.fresh, scan.to_call) == (2, 1, 0)
    assert world.profiler.calls == [] and run.rederived == 2 and not run.error
    for text in (ALEX, HOTEL):
        doc = world.docs[fid(text)]
        assert doc["place_keys"] == ["pl:2000", "co:united_states"] and doc["places_unresolved"] == []
        assert doc["profile_derivation"].startswith(base_derivation() + "|a")
    assert world.docs[fid(HANNA)] == porto_before
    assert world.docs[fid(HANNA)]["profile_derivation"] == base_derivation()


async def test_a_second_run_after_the_re_derivation_finds_everything_fresh():
    store = Answers()
    world = build(store)
    for text in (ALEX, HANNA):
        await world.seed(text)
    await world.profile_everything()
    await store.record(ACC, ROME)
    await world.run()
    world.profiler.calls.clear()

    scan = await world.service.scan(ACC)

    assert scan.fresh == 2 and scan.to_call == 0


async def test_unreadable_answers_end_the_step_with_nothing_called_or_written(caplog):
    world = build(Answers(fail=True))
    await world.seed(ALEX)

    with caplog.at_level(logging.ERROR):
        run = await world.run()

    assert run.error is True and world.profiler.calls == []
    assert world.docs[fid(ALEX)].get("profile_fingerprint") is None
    assert any("profile answers unreadable account=" + ACC[:8] in r.getMessage() for r in caplog.records)
    assert not any(ALEX in r.getMessage() for r in caplog.records)


async def test_a_called_fact_stores_the_derivation_with_its_new_answers_digest():
    store = Answers()
    await store.record(ACC, ROME)
    world = build(store)
    await world.seed(ALEX)

    run = await world.run()

    model = answer(places=[{"city": "Rome", "country": "", "source": "Rome"}])
    digest = answers_digest(store.answers, model, f"lin-{ALEX}", NAMES)
    doc = world.docs[fid(ALEX)]
    assert run.called == 1 and digest
    assert doc["profile_derivation"] == base_derivation() + "|a" + digest
    assert doc["place_keys"] == ["pl:2000", "co:united_states"]
    assert (await world.service.scan(ACC)).fresh == 1


async def test_no_answers_store_is_the_behaviour_before_the_answers():
    world = build(None)
    await world.seed(ALEX)
    await world.run()
    assert world.docs[fid(ALEX)]["profile_derivation"] == base_derivation()
    assert world.docs[fid(ALEX)]["places_unresolved"] == [{"city": "Rome", "country": ""}]
