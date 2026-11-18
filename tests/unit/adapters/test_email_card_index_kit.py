"""EmailCardIndex kit (email index v2 §4, §5; slice 8): the port's docstrings checked against `FirestoreEmailCardIndex` on
the evaluating Firestore fake, with the index refusal on (`config/firestore.indexes.json`), and against
`InMemoryEmailCardIndex`. `backend` is the one fixture that selects the index. Invented cast only; every card is built by
the card core."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Optional, Sequence

import pytest
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_email_card_index import FirestoreEmailCardIndex
from src.adapters.in_memory_email_card_index import InMemoryEmailCardIndex
from src.domain.email_card import CardState, EmailCard
from src.domain.email_card_document import card_document
from src.ports.email_card_index import EmailCardIndex
from tests.unit.domain.email_search_fixtures import booking, invoice, policy, school_notice

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS = "eval_email_facts"
USER, OTHER = "usr-1", "usr-2"
V1 = {"text": "Hotel booking RM-55120 confirmed.", "state": "current", "vector": Vector([1.0, 0.0]),
      "tags": ["travel"], "category": "travel"}


class Firestore:
    def __init__(self, kit_db) -> None:
        self.db = kit_db(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1"})
        self.index: EmailCardIndex = FirestoreEmailCardIndex(
            self.db, SimpleNamespace(domain_email_facts_collection=FACTS))

    async def put(self, user_id: str, card: EmailCard, vector: Optional[Sequence[float]] = None) -> None:
        doc = {**V1, "user_id": user_id, **card_document(card)}
        if vector is not None:
            doc["card_vector"] = Vector(list(vector))
        await self.db.collection(FACTS).document(f"{user_id}_{card.mail.email_id}").set(doc)

    async def put_v1_only(self, user_id: str, email_id: str) -> None:
        await self.db.collection(FACTS).document(f"{user_id}_{email_id}").set({**V1, "user_id": user_id})


class InMemory:
    def __init__(self) -> None:
        self.memory = InMemoryEmailCardIndex()
        self.index: EmailCardIndex = self.memory

    async def put(self, user_id: str, card: EmailCard, vector: Optional[Sequence[float]] = None) -> None:
        self.memory.add(user_id, card, vector)

    async def put_v1_only(self, user_id: str, email_id: str) -> None:
        self.memory.add_uncarded(user_id, email_id)


@pytest.fixture(params=[Firestore, InMemory], ids=["firestore", "in_memory"])
async def backend(request, kit_db):
    b = Firestore(kit_db) if request.param is Firestore else InMemory()
    await b.put(USER, booking(), [1.0, 0.0])
    await b.put(USER, invoice(), [0.0, 1.0])
    await b.put(USER, school_notice(), [0.7, 0.7])
    await b.put(USER, replace(policy(), state=CardState.EMPTY), [1.0, 0.0])
    await b.put(OTHER, replace(booking(), mail=replace(booking().mail, email_id="msg_other")), [1.0, 0.0])
    return b


def _ids(cards):
    return sorted(c.mail.email_id for c in cards)


def test_both_implement_the_port():
    assert issubclass(FirestoreEmailCardIndex, EmailCardIndex) and issubclass(InMemoryEmailCardIndex, EmailCardIndex)


async def test_every_read_shape_is_served_by_a_declared_index(kit_db):
    """The fake refuses a query no index in config/firestore.indexes.json serves (the vector one included)."""
    b = Firestore(kit_db)
    await b.put(USER, booking(), [1.0, 0.0])
    for field in ("about_keys", "name_keys", "people_keys"):
        await b.index.find_by_keys(USER, field, ["l:type:booking"], 10)
    assert len(await b.index.list_in_months(USER, ["m:2025-06"], 10)) == 1
    assert len(await b.index.find_nearest(USER, [1.0, 0.0], 10)) == 1
    assert await b.index.count_carded(USER) == 1 and await b.index.count_uncarded(USER) == 0


async def test_only_carded_cards_are_returned(backend):
    idx = backend.index
    assert _ids(await idx.find_by_keys(USER, "about_keys", ["l:type:booking", "l:type:insurance_policy"], 10)) == [
        "msg_rome01"]
    assert _ids(await idx.list_in_months(USER, ["m:2026-03", "m:2027-03"], 10)) == ["msg_school_meeting"]
    assert "msg_policy01" not in _ids(c for c, _ in await idx.find_nearest(USER, [1.0, 0.0], 10))


async def test_another_users_cards_are_never_returned(backend):
    idx = backend.index
    assert _ids(await idx.find_by_keys(USER, "about_keys", ["l:type:booking"], 10)) == ["msg_rome01"]
    assert _ids(await idx.find_by_keys(OTHER, "about_keys", ["l:type:booking"], 10)) == ["msg_other"]
    assert _ids(c for c, _ in await idx.find_nearest(OTHER, [1.0, 0.0], 10)) == ["msg_other"]
    assert await idx.find_by_keys("nobody", "about_keys", ["l:type:booking"], 10) == []


async def test_a_v1_document_with_no_card_fields_is_never_returned(backend):
    await backend.put_v1_only(USER, "msg_v1_only")
    idx = backend.index
    assert "msg_v1_only" not in _ids(await idx.list_in_months(USER, ["m:2025-06", "m:2026-09"], 10))
    assert "msg_v1_only" not in _ids(c for c, _ in await idx.find_nearest(USER, [1.0, 0.0], 10))
    assert await idx.count_carded(USER) == 3


async def test_each_key_field_is_read(backend):
    idx = backend.index
    assert _ids(await idx.find_by_keys(USER, "name_keys", list(invoice().name_keys)[:5], 10)) == ["msg_invoice01"]
    assert _ids(await idx.find_by_keys(USER, "people_keys", ["e:jamie"], 10)) == ["msg_school_meeting"]
    assert _ids(await idx.find_by_keys(USER, "about_keys", ["v:fa0137"], 10)) == ["msg_invoice01"]


async def test_thirty_one_keys_and_an_unknown_field_are_refused(backend):
    idx = backend.index
    thirty = [f"k:{i}" for i in range(30)]
    assert await idx.find_by_keys(USER, "about_keys", thirty, 10) == []
    with pytest.raises(ValueError):
        await idx.find_by_keys(USER, "about_keys", [*thirty, "k:30"], 10)
    with pytest.raises(ValueError):
        await idx.find_by_keys(USER, "template_key", ["x"], 10)
    with pytest.raises(ValueError):
        await idx.list_in_months(USER, [f"m:2020-{i:02d}" for i in range(31)], 10)


async def test_no_keys_or_no_room_reads_nothing(backend):
    idx = backend.index
    assert await idx.find_by_keys(USER, "about_keys", [], 10) == []
    assert await idx.find_by_keys(USER, "about_keys", ["l:type:booking"], 0) == []
    assert len(await idx.find_by_keys(USER, "about_keys", ["l:domain:education", "l:type:booking"], 1)) == 1


async def test_the_nearest_come_first_by_cosine_distance(backend):
    found = await backend.index.find_nearest(USER, [1.0, 0.1], 10)
    assert [c.mail.email_id for c, _ in found] == ["msg_rome01", "msg_school_meeting", "msg_invoice01"]
    distances = [d for _, d in found]
    assert distances == sorted(distances) and distances[0] < 0.01
    assert len(await backend.index.find_nearest(USER, [1.0, 0.1], 2)) == 2


async def test_a_card_without_a_vector_is_absent_from_the_nearest(backend):
    await backend.put(USER, replace(invoice(), mail=replace(invoice().mail, email_id="msg_invoice02")))
    found = [c.mail.email_id for c, _ in await backend.index.find_nearest(USER, [0.0, 1.0], 10)]
    assert "msg_invoice02" not in found and "msg_invoice01" in found
    assert await backend.index.count_carded(USER) == 4


async def test_the_month_read(backend):
    idx = backend.index
    assert _ids(await idx.list_in_months(USER, ["m:2025-08"], 10)) == ["msg_rome01"]
    assert _ids(await idx.list_in_months(USER, ["m:2025-06", "m:2026-09"], 10)) == ["msg_invoice01", "msg_rome01"]
    assert await idx.list_in_months(USER, ["m:2019-01"], 10) == []


async def test_both_counts(backend):
    idx = backend.index
    assert await idx.count_carded(USER) == 3 and await idx.count_carded(OTHER) == 1
    assert await idx.count_uncarded(USER) == 0
    await backend.put_v1_only(USER, "msg_new1")
    await backend.put_v1_only(USER, "msg_new2")
    await backend.put(USER, replace(policy(), state=CardState.PENDING))
    assert await idx.count_uncarded(USER) == 3
    await backend.put(USER, policy())
    await backend.put(USER, replace(booking(), mail=replace(booking().mail, email_id="msg_new1")))
    assert await idx.count_uncarded(USER) == 1 and await idx.count_carded(USER) == 5
    assert await idx.count_uncarded(OTHER) == 0
