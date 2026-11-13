"""InMemoryEmailCardIndex against the EmailCardIndex contract (search slice 4): carded cards only, one user's cards,
the argument errors, cosine order, the month and key reads and both counts. Invented cast only."""
from dataclasses import replace

import pytest

from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.domain.email_card import CardState
from tests.unit.domain.email_search_fixtures import booking, invoice, policy, school_notice

USER, OTHER = "usr-1", "usr-2"


def _ids(cards):
    return [c.mail.email_id for c in cards]


@pytest.fixture
def index():
    idx = InMemoryEmailCardIndex()
    idx.add(USER, booking(), [1.0, 0.0])
    idx.add(USER, invoice(), [0.0, 1.0])
    idx.add(USER, school_notice(), [0.7, 0.7])
    idx.add(USER, replace(policy(), state=CardState.EMPTY), [1.0, 0.0])
    idx.add(OTHER, replace(booking(), mail=replace(booking().mail, email_id="msg_other")), [1.0, 0.0])
    return idx


async def test_only_carded_cards_of_the_user_are_read(index):
    assert _ids(await index.find_by_keys(USER, "about_keys", ["l:type:booking", "l:type:insurance_policy"], 10)) == [
        "msg_rome01"]
    assert _ids(await index.find_by_keys(OTHER, "about_keys", ["l:type:booking"], 10)) == ["msg_other"]
    assert await index.find_by_keys("nobody", "about_keys", ["l:type:booking"], 10) == []


async def test_every_card_state_but_carded_is_hidden(index):
    for state in (CardState.PENDING, CardState.EMPTY, CardState.ABANDONED, CardState.SOURCE_GONE):
        idx = InMemoryEmailCardIndex()
        idx.add(USER, replace(booking(), state=state), [1.0, 0.0])
        assert await idx.find_by_keys(USER, "about_keys", ["l:type:booking"], 10) == []
        assert await idx.list_in_months(USER, ["m:2025-06"], 10) == []
        assert await idx.find_nearest(USER, [1.0, 0.0], 10) == []
        assert await idx.count_carded(USER) == 0


async def test_each_key_field_is_read(index):
    assert _ids(await index.find_by_keys(USER, "name_keys", list(invoice().name_keys)[:5], 10)) == ["msg_invoice01"]
    assert _ids(await index.find_by_keys(USER, "people_keys", ["e:jamie"], 10)) == ["msg_school_meeting"]


async def test_a_key_list_over_thirty_and_an_unknown_field_are_refused(index):
    thirty = [f"k:{i}" for i in range(30)]
    assert await index.find_by_keys(USER, "about_keys", thirty, 10) == []
    with pytest.raises(ValueError):
        await index.find_by_keys(USER, "about_keys", [*thirty, "k:30"], 10)
    with pytest.raises(ValueError):
        await index.find_by_keys(USER, "template_key", ["x"], 10)
    with pytest.raises(ValueError):
        await index.list_in_months(USER, [f"m:2020-{i:02d}" for i in range(31)], 10)


async def test_the_limit_cuts_a_read(index):
    assert len(await index.find_by_keys(USER, "about_keys", ["l:domain:education", "l:type:booking"], 1)) == 1
    assert await index.find_by_keys(USER, "about_keys", ["l:type:booking"], 0) == []


async def test_a_month_read_returns_the_cards_with_that_month_key(index):
    assert _ids(await index.list_in_months(USER, ["m:2025-08"], 10)) == ["msg_rome01"]
    assert _ids(await index.list_in_months(USER, ["m:2026-03"], 10)) == ["msg_school_meeting"]
    assert sorted(_ids(await index.list_in_months(USER, ["m:2025-06", "m:2026-09"], 10))) == [
        "msg_invoice01", "msg_rome01"]
    assert await index.list_in_months(USER, ["m:2019-01"], 10) == []


async def test_the_nearest_come_first_by_cosine_distance(index):
    found = await index.find_nearest(USER, [1.0, 0.1], 10)
    assert _ids([c for c, _ in found]) == ["msg_rome01", "msg_school_meeting", "msg_invoice01"]
    distances = [d for _, d in found]
    assert distances == sorted(distances) and distances[0] < 0.01
    assert len(await index.find_nearest(USER, [1.0, 0.1], 2)) == 2


async def test_a_card_with_no_vector_is_not_a_neighbour():
    idx = InMemoryEmailCardIndex()
    idx.add(USER, booking())
    idx.add(USER, invoice(), [0.0, 1.0])
    assert _ids([c for c, _ in await idx.find_nearest(USER, [0.0, 1.0], 5)]) == ["msg_invoice01"]
    assert await idx.count_carded(USER) == 2


async def test_the_counts(index):
    assert await index.count_carded(USER) == 3 and await index.count_carded(OTHER) == 1
    assert await index.count_uncarded(USER) == 0
    index.add_uncarded(USER, "msg_new1")
    index.add_uncarded(USER, "msg_new2")
    index.add(USER, replace(policy(), state=CardState.PENDING))
    assert await index.count_uncarded(USER) == 3
    index.add(USER, replace(policy(), state=CardState.CARDED))
    index.add(USER, replace(booking(), mail=replace(booking().mail, email_id="msg_new1")))
    assert await index.count_uncarded(USER) == 1 and await index.count_carded(USER) == 5
    assert await index.count_uncarded(OTHER) == 0
