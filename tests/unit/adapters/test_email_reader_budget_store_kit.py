"""FirestoreEmailReaderBudget kit (email index v2, slice 5): the daily page budget of the email index's paid scan
reader, run over the evaluating Firestore fake with its transactions."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, Optional


from src.adapters.firestore_email_reader_budget import FirestoreEmailReaderBudget
from src.ports.reader_page_budget import ReaderPageBudget

PAGES = "eval_email_reader_pages"
DAY1 = datetime(2026, 11, 16, 23, 59, tzinfo=timezone.utc).timestamp()
DAY2 = datetime(2026, 11, 17, 0, 1, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def build(kit_db, cap: int, clock: Clock):
    db = kit_db()
    store = FirestoreEmailReaderBudget(db, SimpleNamespace(email_reader_pages_collection=PAGES), cap, clock=clock)
    return db, store


async def doc(db, user_id: str) -> Optional[Dict[str, Any]]:
    return (await db.collection(PAGES).document(user_id).get()).to_dict()


def test_it_implements_the_port():
    assert issubclass(FirestoreEmailReaderBudget, ReaderPageBudget)


async def test_a_booking_within_the_cap_is_granted_and_stored(kit_db):
    db, store = build(kit_db, 5, Clock(DAY1))
    assert await store.reserve("u-alex", 3) is True
    assert await store.reserve("u-alex", 2) is True
    assert await doc(db, "u-alex") == {"user_id": "u-alex", "day": "2026-11-16", "pages": 5}


async def test_a_booking_over_the_cap_is_refused_and_the_document_is_unchanged(kit_db):
    db, store = build(kit_db, 5, Clock(DAY1))
    await store.reserve("u-alex", 3)
    before = await doc(db, "u-alex")
    assert await store.reserve("u-alex", 3) is False
    assert await doc(db, "u-alex") == before


async def test_the_next_utc_day_starts_from_zero(kit_db):
    clock = Clock(DAY1)
    db, store = build(kit_db, 5, clock)
    await store.reserve("u-alex", 5)
    assert await store.reserve("u-alex", 1) is False
    clock.now = DAY2
    assert await store.reserve("u-alex", 4) is True
    assert await doc(db, "u-alex") == {"user_id": "u-alex", "day": "2026-11-17", "pages": 4}


async def test_users_do_not_share_a_counter(kit_db):
    _, store = build(kit_db, 3, Clock(DAY1))
    assert await store.reserve("u-alex", 3) is True
    assert await store.reserve("u-bea", 3) is True


async def test_a_cap_of_zero_refuses_everything(kit_db):
    db, store = build(kit_db, 0, Clock(DAY1))
    assert await store.reserve("u-alex", 1) is False
    assert await doc(db, "u-alex") is None


async def test_two_concurrent_bookings_of_three_pages_against_five_grant_one(kit_db):
    db, store = build(kit_db, 5, Clock(DAY1))
    granted = await asyncio.gather(store.reserve("u-alex", 3), store.reserve("u-alex", 3))
    assert sorted(granted) == [False, True]
    assert (await doc(db, "u-alex"))["pages"] == 3


async def test_a_store_failure_refuses_and_logs_once(caplog):
    class Broken:
        def collection(self, name):
            return SimpleNamespace(document=lambda _id: object())

        def transaction(self):
            raise RuntimeError("down")

    store = FirestoreEmailReaderBudget(Broken(), SimpleNamespace(email_reader_pages_collection=PAGES), 5,
                                       clock=Clock(DAY1))
    with caplog.at_level("ERROR"):
        assert await store.reserve("u-alex", 1) is False
    assert sum("booking failed" in r.getMessage() for r in caplog.records) == 1


async def test_a_document_that_cannot_be_addressed_refuses_without_raising(caplog):
    class Broken:
        def collection(self, name):
            def document(_id):
                raise RuntimeError("bad id")
            return SimpleNamespace(document=document)

    store = FirestoreEmailReaderBudget(Broken(), SimpleNamespace(email_reader_pages_collection=PAGES), 5,
                                       clock=Clock(DAY1))
    with caplog.at_level("ERROR"):
        assert await store.reserve("u-alex", 1) is False
    assert sum("booking failed" in r.getMessage() for r in caplog.records) == 1
