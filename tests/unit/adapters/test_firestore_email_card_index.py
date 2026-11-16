"""FirestoreEmailCardIndex at its boundary (email index v2 §4; slice 8): every read projects the card fields only (never
the v1 vectors or text), the kNN distance comes back under the projection, an unreadable card is skipped with a WARNING
naming the mail id only, and a document of another user is skipped with an ERROR. The evaluating fake for what it can
show; a scripted query only where the fake's own filter would hide the case (a foreign `user_id` in the answer).
Invented cast only."""
import logging
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List

import pytest
from google.cloud.firestore_v1.vector import Vector

from src.adapters.firestore_email_card_index import CARD_DISTANCE, READ_FIELDS, FirestoreEmailCardIndex
from src.domain.email_card_document import card_document
from tests.support.firestore_eval import EvalDb
from tests.unit.domain.email_search_fixtures import booking, invoice

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS = "eval_email_facts"
USER = "usr-1"
V1_FIELDS = ("text", "vector", "tags_vector", "tags", "category", "metadata")


def _doc(card, vector=None) -> Dict[str, Any]:
    doc = {"user_id": USER, "text": "v1 text of the mail", "vector": Vector([1.0, 0.0]),
           "tags_vector": Vector([0.0, 1.0]), "tags": ["travel"], "category": "travel", "metadata": {"k": "v"},
           **card_document(card)}
    if vector is not None:
        doc["card_vector"] = Vector(vector)
    return doc


@pytest.fixture
async def world():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1"})
    col = db.collection(FACTS)
    await col.document(f"{USER}_msg_rome01").set(_doc(booking(), [1.0, 0.0]))
    await col.document(f"{USER}_msg_invoice01").set(_doc(invoice(), [0.0, 1.0]))
    return SimpleNamespace(db=db, col=col,
                           index=FirestoreEmailCardIndex(db, SimpleNamespace(domain_email_facts_collection=FACTS)))


def _selects(col) -> List[tuple]:
    return [op[1] for ops in col.executed for op in ops if op[0] == "select"]


async def test_every_card_read_projects_the_card_fields_only(world):
    await world.index.find_by_keys(USER, "about_keys", ["l:type:booking"], 10)
    await world.index.list_in_months(USER, ["m:2025-06"], 10)
    await world.index.find_nearest(USER, [1.0, 0.0], 5)
    selects = _selects(world.col)
    assert len(selects) == 3
    for fields in selects:
        assert set(fields) - {CARD_DISTANCE} == set(READ_FIELDS)
        assert not set(fields) & set(V1_FIELDS) and "card_vector" not in fields


async def test_every_read_filters_on_the_user_and_the_carded_state(world):
    await world.index.find_by_keys(USER, "name_keys", ["fa0137"], 10)
    await world.index.find_nearest(USER, [1.0, 0.0], 5)
    for ops in world.col.executed:
        wheres = {(op[1], op[2], op[3] if not isinstance(op[3], list) else None) for op in ops if op[0] == "where"}
        assert ("user_id", "==", USER) in wheres and ("card_state", "==", "carded") in wheres


async def test_the_nearest_carry_their_cosine_distance(world):
    found = await world.index.find_nearest(USER, [1.0, 0.0], 5)
    assert [(c.mail.email_id, round(d, 6)) for c, d in found] == [("msg_rome01", 0.0), ("msg_invoice01", 1.0)]


async def test_an_unreadable_card_is_skipped_with_a_warning_naming_the_mail_only(world, caplog):
    broken = _doc(booking(), [1.0, 0.0])
    broken["card"] = {"profile": {}}      # no mail block: card_from_document refuses it
    await world.col.document(f"{USER}_msg_broken").set(broken)
    with caplog.at_level(logging.WARNING):
        found = await world.index.find_nearest(USER, [1.0, 0.0], 5)
        by_key = await world.index.find_by_keys(USER, "about_keys", ["l:type:booking"], 10)
    assert [c.mail.email_id for c, _ in found] == ["msg_rome01", "msg_invoice01"]
    assert [c.mail.email_id for c in by_key] == ["msg_rome01"]
    lines = [r.getMessage() for r in caplog.records if "unreadable card" in r.getMessage()]
    assert lines and all(line == "[EmailCardIndex] unreadable card msg_broken" for line in lines)


class _Snap:
    def __init__(self, doc_id: str, data: Dict[str, Any]) -> None:
        self.id, self._data = doc_id, data

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)


class _ScriptedQuery:
    """The SDK query surface the adapter uses, answering every read with the scripted snapshots (a backend that
    returned a document its filter should have excluded)."""

    def __init__(self, snaps: List[_Snap]) -> None:
        self.snaps = snaps

    def where(self, *args: Any, **kwargs: Any) -> "_ScriptedQuery":
        return self

    def select(self, fields: Any) -> "_ScriptedQuery":
        return self

    def limit(self, n: int) -> "_ScriptedQuery":
        return self

    def find_nearest(self, **kwargs: Any) -> "_ScriptedQuery":
        return self

    async def get(self) -> List[_Snap]:
        return self.snaps


async def test_a_document_of_another_user_is_skipped_with_an_error(caplog):
    mine = {**_doc(booking()), CARD_DISTANCE: 0.1}
    foreign = {**_doc(invoice()), "user_id": "usr-2", CARD_DISTANCE: 0.05}
    query = _ScriptedQuery([_Snap("usr-2_msg_invoice01", foreign), _Snap(f"{USER}_msg_rome01", mine)])
    index = FirestoreEmailCardIndex(SimpleNamespace(collection=lambda name: query),
                                    SimpleNamespace(domain_email_facts_collection=FACTS))
    with caplog.at_level(logging.ERROR):
        nearest = await index.find_nearest(USER, [1.0, 0.0], 5)
        by_key = await index.find_by_keys(USER, "about_keys", ["l:type:booking"], 10)
    assert [c.mail.email_id for c, _ in nearest] == ["msg_rome01"]
    assert [c.mail.email_id for c in by_key] == ["msg_rome01"]
    errors = [r for r in caplog.records if r.levelno == logging.ERROR and "another user" in r.getMessage()]
    assert len(errors) == 2 and all("msg_invoice01" not in r.getMessage() for r in errors)
