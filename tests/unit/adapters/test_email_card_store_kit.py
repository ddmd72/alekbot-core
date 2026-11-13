"""EmailCardStore kit: the docstrings of `src/ports/email_card_store.py` checked against `FirestoreEmailCardStore` on the
evaluating Firestore fake and against the test fake `tests/support/email_card_store.py`. Read-through: `read_expect` and
the stored document. `backend` is the one fixture that selects the store. Invented mail (Albergo Immaginario RM-55120)."""
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

import pytest

from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
from src.domain.email_card_lifecycle import CardExpect, CardWriteResult
from src.ports.email_card_store import EmailCardStore
from tests.support.email_card_store import FakeEmailCardStore
from tests.support.firestore_eval import EvalDb, eval_transactions, stored_vector

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS = "eval_email_facts"
V1 = {"text": "Hotel booking RM-55120 confirmed.", "state": "current", "consolidated_at": None, "vector": [1.0, 0.0]}
CARDED = {"card_state": "carded", "card_version": 1, "people_digest": "dg", "card_model": "m", "card_failures": 0,
          "card_reason": "", "card_vector_pending": False, "about_keys": ["v:rm55120"]}


class Firestore:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1"})
        self.store: EmailCardStore = FirestoreEmailCardStore(self.db, SimpleNamespace(domain_email_facts_collection=FACTS))

    async def seed(self, user_id: str, email_id: str) -> None:
        await self.db.collection(FACTS).document(f"{user_id}_{email_id}").set(dict(V1))

    async def doc(self, user_id: str, email_id: str) -> Optional[Dict[str, Any]]:
        data = (await self.db.collection(FACTS).document(f"{user_id}_{email_id}").get()).to_dict()
        if data is not None and "card_vector" in data:
            data["card_vector"] = stored_vector(data["card_vector"])
        return data


class Fake:
    def __init__(self) -> None:
        self.store = FakeEmailCardStore()

    async def seed(self, user_id: str, email_id: str) -> None:
        self.store.seed(user_id, email_id, **V1)

    async def doc(self, user_id: str, email_id: str) -> Optional[Dict[str, Any]]:
        doc = self.store.docs.get((user_id, email_id))
        return None if doc is None else dict(doc)


@pytest.fixture(params=[Firestore, Fake], ids=["firestore", "fake"])
def backend(request):
    with eval_transactions():
        yield request.param()


def test_both_implement_the_port():
    assert issubclass(FirestoreEmailCardStore, EmailCardStore) and issubclass(FakeEmailCardStore, EmailCardStore)


async def test_an_absent_expectation_writes_the_card_fields_and_leaves_the_v1_fields(backend):
    await backend.seed("u-alex", "m1")
    result = await backend.store.write_card("u-alex", "m1", CARDED, [0.5, 0.5], CardExpect.absent())
    doc = await backend.doc("u-alex", "m1")
    assert result is CardWriteResult.WRITTEN
    assert {k: doc[k] for k in V1} == V1
    assert doc["card_state"] == "carded" and doc["about_keys"] == ["v:rm55120"] and doc["card_vector"] == [0.5, 0.5]


async def test_a_stale_expectation_conflicts_and_changes_nothing(backend):
    await backend.seed("u-alex", "m1")
    await backend.store.write_card("u-alex", "m1", CARDED, None, CardExpect.absent())
    before = await backend.doc("u-alex", "m1")
    again = await backend.store.write_card("u-alex", "m1", {**CARDED, "card_state": "pending"}, None,
                                           CardExpect.absent())
    wrong_digest = await backend.store.write_card("u-alex", "m1", {**CARDED, "card_state": "pending"}, None,
                                                  CardExpect(1, "other"))
    assert (again, wrong_digest) == (CardWriteResult.CONFLICT, CardWriteResult.CONFLICT)
    assert await backend.doc("u-alex", "m1") == before


async def test_the_stored_pair_as_expectation_writes(backend):
    await backend.seed("u-alex", "m1")
    await backend.store.write_card("u-alex", "m1", CARDED, None, CardExpect.absent())
    result = await backend.store.write_card("u-alex", "m1", {**CARDED, "card_state": "empty"}, None,
                                            CardExpect(1, "dg"))
    assert result is CardWriteResult.WRITTEN and (await backend.doc("u-alex", "m1"))["card_state"] == "empty"


async def test_a_missing_document_is_reported_and_never_created(backend):
    result = await backend.store.write_card("u-alex", "m404", CARDED, [0.5, 0.5], CardExpect.absent())
    assert result is CardWriteResult.MISSING and await backend.doc("u-alex", "m404") is None


async def test_another_users_document_is_never_written(backend):
    await backend.seed("u-clara", "m1")
    result = await backend.store.write_card("u-alex", "m1", CARDED, None, CardExpect.absent())
    assert result is CardWriteResult.MISSING and "card_state" not in await backend.doc("u-clara", "m1")


@pytest.mark.parametrize("bad", [{"text": "x"}, {"consolidated_at": None}, {"card_state": "carded", "state": "x"},
                                 {"card_vector": [1.0]}])
async def test_a_key_outside_the_card_fields_or_a_vector_among_the_fields_is_refused(backend, bad):
    await backend.seed("u-alex", "m1")
    before = await backend.doc("u-alex", "m1")
    with pytest.raises(ValueError):
        await backend.store.write_card("u-alex", "m1", bad, None, CardExpect.absent())
    assert await backend.doc("u-alex", "m1") == before


async def test_card_vector_is_set_from_the_vector_and_removed_when_none(backend):
    await backend.seed("u-alex", "m1")
    await backend.store.write_card("u-alex", "m1", CARDED, [0.25, 0.75], CardExpect.absent())
    assert (await backend.doc("u-alex", "m1"))["card_vector"] == [0.25, 0.75]
    await backend.store.write_card("u-alex", "m1", CARDED, None, CardExpect(1, "dg"))
    assert "card_vector" not in await backend.doc("u-alex", "m1")


async def test_read_expect_maps_stored_documents_and_leaves_absent_ones_out(backend):
    await backend.seed("u-alex", "m1")
    await backend.seed("u-alex", "m2")
    await backend.seed("u-clara", "m9")
    await backend.store.write_card("u-alex", "m1", CARDED, None, CardExpect.absent())
    got = await backend.store.read_expect("u-alex", ["m1", "m2", "m3", "m9"])
    assert got == {"m1": CardExpect(1, "dg"), "m2": CardExpect.absent()}
    assert await backend.store.read_expect("u-alex", []) == {}


async def test_the_firestore_store_writes_to_the_v1_collection_under_the_user_prefixed_id():
    fs = Firestore()
    await fs.seed("u-alex", "m1")
    await fs.store.write_card("u-alex", "m1", CARDED, None, CardExpect.absent())
    assert sorted(fs.db.collection(FACTS).docs) == ["u-alex_m1"]
