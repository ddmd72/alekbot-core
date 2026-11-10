"""The confirmed people list in the profile step (fact profile RFC §4.2, §4.5, §4.6): the whole list goes with every call,
the fingerprint holds only the entries a fact names, an unreadable list ends the step, a date without a year takes the
recording year and a needs-review answer is written and counted. Invented cast only."""
import logging
from typing import Any, List, Optional

import pytest

from src.domain.confirmed_people import ConfirmedPeople, parse_confirmed_people
from src.domain.fact_profile_fingerprint import named_people, profile_fingerprint
from src.ports.confirmed_people_repository import ConfirmedPeopleRepository
from tests.support.fact_kit import ACC, OTHER
from tests.unit.services.test_fact_profile_service import World, answer, fid, ok

ANA = "The user has a daughter, Ana"
CLARA = "Weekend in Coimbra with Clara"
GYM = "The Iron Temple gym closes on Sunday"
CONFERENCE = "Hamburg conference Oct 15"
MOTHER = "The user's mother likes tea"


def household(ana_relation: str = "younger daughter") -> ConfirmedPeople:
    return parse_confirmed_people({
        "user": {"name": "Alex Doe", "forms": ["Alex", "Bo"]},
        "people": [{"name": "Clara Moreira", "forms": ["Clara"], "relation": "partner"},
                   {"name": "Ana Doe", "forms": [], "relation": ana_relation},
                   {"name": "Maria Doe", "forms": ["Mariia"], "relation": "mother"}]})


class Lists(ConfirmedPeopleRepository):
    def __init__(self, people: Optional[ConfirmedPeople] = None, fail: bool = False) -> None:
        self.people, self.fail, self.reads = people, fail, 0

    async def get(self, account_id: str) -> Optional[ConfirmedPeople]:
        self.reads += 1
        if self.fail:
            raise RuntimeError("firestore unavailable")
        return self.people

    async def replace(self, account_id: str, people: ConfirmedPeople, source: str) -> None:
        self.people = people


class Recording:
    """Wraps the world's profiler to record the people each call carried."""

    def __init__(self, inner: Any) -> None:
        self.inner, self.seen = inner, {}

    async def profile(self, account_id, text, metadata, people=()):
        self.seen[text] = tuple(people)
        return await self.inner.profile(account_id, text, metadata, people)


def build(lists: Optional[Lists]):
    world = World(confirmed_people=lists)
    recorder = Recording(world.profiler)
    world.service._profiler = recorder
    return world, recorder


async def test_every_call_carries_the_whole_list_and_the_stored_fingerprint_holds_the_named_entries_only():
    lists = Lists(household())
    world, recorder = build(lists)
    for text in (ANA, CLARA, GYM):
        await world.seed(text)

    run = await world.run()

    assert run.called == 3 and not run.error and lists.reads == 1
    whole = household().entries()
    assert all(recorder.seen[text] == whole for text in (ANA, CLARA, GYM))
    for text, named in ((ANA, ["Ana Doe"]), (CLARA, ["Clara Moreira"]), (GYM, [])):
        expected = profile_fingerprint(text, None, named_people(household(), text, None))
        assert world.docs[fid(text)]["profile_fingerprint"] == expected
        assert [e.name for e in named_people(household(), text, None)] == named


async def test_a_second_run_with_the_same_list_makes_no_call():
    world, _ = build(Lists(household()))
    for text in (ANA, CLARA, GYM):
        await world.seed(text)
    await world.run()
    world.profiler.calls.clear()

    run = await world.run()

    assert world.profiler.calls == [] and run.called == 0 and run.scan.fresh == 3


async def test_a_list_edit_re_calls_only_the_facts_that_name_the_edited_person():
    lists = Lists(household())
    world, _ = build(lists)
    for text in (ANA, CLARA, GYM):
        await world.seed(text)
    await world.run()
    world.profiler.calls.clear()

    lists.people = household(ana_relation="daughter")
    scan = await world.service.scan(ACC)
    run = await world.run()

    assert scan.stale_input == 1 and scan.to_call == 1
    assert world.profiler.calls == [ANA] and run.written == 1


async def test_a_fact_naming_nobody_is_not_re_called_by_the_first_list_write():
    lists = Lists(None)
    world, _ = build(lists)
    for text in (ANA, GYM):
        await world.seed(text)
    await world.run()
    world.profiler.calls.clear()

    lists.people = household()
    await world.run()

    assert world.profiler.calls == [ANA]


async def test_an_unreadable_list_ends_the_step_with_nothing_called_or_written(caplog):
    world, _ = build(Lists(fail=True))
    await world.seed(ANA)

    with caplog.at_level(logging.ERROR):
        run = await world.run()

    assert run.error is True and world.profiler.calls == []
    assert world.docs[fid(ANA)].get("profile_fingerprint") is None
    assert any("confirmed people unreadable account=" + ACC[:8] in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
    assert not any(ANA in r.getMessage() for r in caplog.records)


async def test_an_unreadable_list_raises_from_a_scan():
    world, _ = build(Lists(fail=True))
    with pytest.raises(RuntimeError):
        await world.service.scan(ACC)


async def test_no_repository_is_the_behaviour_before_the_list():
    world, recorder = build(None)
    await world.seed(ANA)

    await world.run()

    assert recorder.seen[ANA] == ()
    assert world.docs[fid(ANA)]["profile_fingerprint"] == profile_fingerprint(ANA, None)


async def test_an_account_without_a_list_calls_with_no_people():
    world, recorder = build(Lists(None))
    await world.seed(ANA)
    await world.run()
    assert recorder.seen[ANA] == ()


async def test_a_date_printed_without_a_year_is_filled_from_the_recording_year():
    world, _ = build(Lists(None))
    await world.seed(CONFERENCE)
    world.profiler.replies[CONFERENCE] = ok(answer(time_role="event", time_points=[
        {"from": "--10-15", "to": "", "precision": "day", "source": "Oct 15"}]))

    await world.run()

    doc = world.docs[fid(CONFERENCE)]
    assert doc["time_points"][0]["from"] == "2026-10-15"     # the seeded fact was created in 2026
    assert doc["profile_answer"]["time_points"][0]["from"] == "--10-15"
    assert doc["profile_derivation"].endswith("|y2026")


async def test_a_needs_review_answer_is_written_counted_and_seen_by_the_next_scan(caplog):
    world, _ = build(Lists(household()))
    await world.seed(ANA)
    world.profiler.replies[ANA] = ok(answer(quality="needs_review", review_items=[
        {"field": "people", "mention": "Ana"}]))

    with caplog.at_level(logging.INFO):
        run = await world.run()

    doc = world.docs[fid(ANA)]
    assert run.written == 1 and run.flagged == 1 and run.failed == 0
    assert doc["profile_review_needed"] is True
    assert doc["profile_review_items"] == [{"field": "people", "mention": "Ana"}]
    assert any("flagged=1" in r.getMessage() for r in caplog.records)
    assert (await world.service.scan(ACC)).needs_review == 1


async def test_a_confident_answer_is_not_flagged():
    world, _ = build(Lists(household()))
    await world.seed(ANA)
    run = await world.run()
    assert run.flagged == 0 and world.docs[fid(ANA)]["profile_review_needed"] is False


async def test_a_relation_only_mention_takes_the_listed_name_and_is_marked_from_the_list():
    world, _ = build(Lists(household()))
    await world.seed(MOTHER)
    world.profiler.replies[MOTHER] = ok(answer(about_person="relative", people=[
        {"name": "Maria Doe", "role": "mother", "source": "mother"}]))

    await world.run()

    person = world.docs[fid(MOTHER)]["people"][0]
    assert (person["name"], person["role"], person["from_list"]) == ("Maria Doe", "mother", True)


async def test_the_second_account_is_not_given_the_first_accounts_list():
    class PerAccount(Lists):
        async def get(self, account_id):
            return household() if account_id == ACC else None

    world, recorder = build(PerAccount())
    await world.seed(ANA, account=OTHER)
    await world.service.run(OTHER, world.until)
    assert recorder.seen[ANA] == ()
