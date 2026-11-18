"""`EmailMemoryBatches` on the v1 repository (email index v2 §6.7, slice 9): the batch Stage 3 reads. Over the
evaluating fake with the declared indexes; the mail is saved by the real `save_batch`, its card by the real
`FirestoreEmailCardStore.write_card`, the consolidation mark by the real `mark_consolidated`. Settled mail comes oldest
first, an unsettled one only once it is older than the wait, a consolidated one never (a later card write leaves the
mark), each once, within the limit and the user."""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import FailedPrecondition

from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect, CardWriteResult, gone_fields, pending_fields, settled_fields
from src.domain.email_memory_candidate import SETTLED_FOR_MEMORY, memory_card
from src.ports.email_memory_batches import EmailMemoryBatches
from src.ports.indexed_email_repository import IndexedEmailRepository
from tests.support.firestore_eval import EvalDb
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
GROUP = "development_domain_email_facts_v1"
T0 = datetime(2026, 11, 10, 8, 0)                      # naive = UTC, as the repository stores and returns it
NOW = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
STALE_BEFORE = datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)    # mail indexed before 12:00 has waited long enough


def _groups() -> dict:
    return {FACTS: GROUP, STATE: "development_email_indexing_state"}


def _world(db):
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    return db, FirestoreIndexedEmailRepository(db, env), FirestoreEmailCardStore(db, env)


@pytest.fixture
def world(kit_db):
    return _world(kit_db(indexes=INDEXES_FILE, groups=_groups()))


def email(email_id: str, hour: int, user_id: str = "user-1", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="acct-1", source="gmail",
                  subject="Booking", from_address="booking@albergo.example",
                  email_date=T0, indexed_at=T0 + timedelta(hours=hour))
    fields.update(over)
    return IndexedEmail(**fields)


def carded(digest: str = "dg"):
    m = mail()
    card = build_card(parse_mail_answer(answer_json()), m, [], build_card_input(m, BOOKING_BODY, [], [], direct=True,
                                                                                 nonce=NONCE).evidence, PEOPLE)
    return settled_fields(card, digest=digest, model="flash-lite", now=NOW, vector_pending=False)


async def card(store, email_id, fields, expect=CardExpect.absent(), user_id="user-1"):
    assert await store.write_card(user_id, email_id, fields, None, expect) is CardWriteResult.WRITTEN


async def batch(repo, limit=10, user_id="user-1"):
    return await repo.memory_batch(user_id, limit, SETTLED_FOR_MEMORY, STALE_BEFORE)


def ids(mails):
    return [m.email.email_id for m in mails]


def test_the_v1_repository_is_both_ports(world):
    _, repo, _ = world
    assert isinstance(repo, IndexedEmailRepository) and isinstance(repo, EmailMemoryBatches)


async def test_settled_mail_comes_oldest_first_and_young_unsettled_mail_waits(world):
    _, repo, store = world
    await repo.save_batch([email("carded", 6), email("empty", 5), email("gone", 7), email("young_pending", 9),
                           email("young_plain", 10)])
    await card(store, "carded", carded())
    empty = {**carded(), "card_state": "empty"}
    await card(store, "empty", empty)
    await card(store, "gone", gone_fields())
    await card(store, "young_pending", pending_fields(reason="outage", failures=0, model=""))

    assert ids(await batch(repo)) == ["empty", "carded", "gone"]


async def test_old_unsettled_mail_enters_after_the_wait_with_its_state(world):
    _, repo, store = world
    await repo.save_batch([email("old_pending", 1), email("old_plain", 2), email("young_pending", 9)])
    await card(store, "old_pending", pending_fields(reason="outage", failures=1, model=""))
    await card(store, "young_pending", pending_fields(reason="outage", failures=1, model=""))

    mails = await batch(repo)

    assert ids(mails) == ["old_pending", "old_plain"]
    assert [(m.card_state, m.card_doc is None) for m in mails] == [("pending", False), ("", True)]


async def test_a_carded_mail_carries_its_card_and_the_v1_record_without_vectors(world):
    db, repo, store = world
    await repo.save_batch([email("carded", 6)])
    await card(store, "carded", carded())
    # a document written before slice 10 still holds the v1 vectors until the owner runs drop_v1_vectors
    await db.collection(FACTS).document("user-1_carded").update(
        {"vector": [1.0, 0.0, 0.0], "tags_vector": [0.0, 1.0, 0.0], "embedding_pending": False})

    (got,) = await batch(repo)

    assert got.card_state == "carded" and got.email.email_id == "carded" and got.email.subject == "Booking"
    assert got.email.from_address == "booking@albergo.example"
    assert not {"vector", "tags_vector", "embedding_pending"} & set(got.email.model_dump())
    assert got.email.indexed_at == T0 + timedelta(hours=6)
    assert memory_card(got).mail.email_id == "msg_rome01"
    assert set(got.card_doc) == {"card_state", "card_version", "template_key", "card"}


async def test_consolidated_mail_never_returns(world):
    _, repo, store = world
    await repo.save_batch([email("done_settled", 1), email("done_plain", 2), email("open", 3)])
    await card(store, "done_settled", carded())
    await repo.mark_consolidated("user-1", ["done_settled", "done_plain"], NOW)

    assert ids(await batch(repo)) == ["open"]


async def test_mail_consolidated_in_the_line_form_and_carded_later_stays_out(world):
    _, repo, store = world
    await repo.save_batch([email("m1", 1)])
    assert ids(await batch(repo)) == ["m1"]                                # it entered as a line (no card yet)
    await repo.mark_consolidated("user-1", ["m1"], NOW)

    await card(store, "m1", carded())

    assert await batch(repo) == []


async def test_mail_carded_consolidated_and_recarded_by_compare_and_set_stays_out(world):
    db, repo, store = world
    await repo.save_batch([email("m1", 6)])
    await card(store, "m1", carded("dg1"))
    (first,) = await batch(repo)
    await repo.mark_consolidated("user-1", [first.email.email_id], NOW)

    await card(store, "m1", carded("dg2"), CardExpect(first.card_doc["card_version"], "dg1"))

    stored = (await db.collection(FACTS).document("user-1_m1").get()).to_dict()
    assert stored["people_digest"] == "dg2" and stored["consolidated_at"] is not None
    assert await batch(repo) == []


async def test_the_limit_holds_across_both_reads_and_each_mail_comes_once(world):
    _, repo, store = world
    # settled and old: both reads find them; settled and young: only the settled read; old plain: only the stale read
    await repo.save_batch([email("s_old_1", 1), email("plain_old", 2), email("s_old_3", 3), email("s_young", 9)])
    for email_id in ("s_old_1", "s_old_3", "s_young"):
        await card(store, email_id, carded())

    assert ids(await batch(repo, limit=10)) == ["s_old_1", "plain_old", "s_old_3", "s_young"]
    assert ids(await batch(repo, limit=3)) == ["s_old_1", "plain_old", "s_old_3"]
    assert ids(await batch(repo, limit=1)) == ["s_old_1"]


async def test_another_users_mail_never_returns(world):
    _, repo, store = world
    await repo.save_batch([email("mine", 1), email("theirs", 0, user_id="user-2")])
    await card(store, "theirs", carded(), user_id="user-2")

    assert ids(await batch(repo)) == ["mine"]
    assert ids(await batch(repo, user_id="user-2")) == ["theirs"]


async def test_the_settled_read_needs_the_declared_composite_index(tmp_path):
    declared = json.loads(INDEXES_FILE.read_text())
    target = [{"fieldPath": f, "order": "ASCENDING"} for f in ("user_id", "consolidated_at", "card_state", "indexed_at")]
    without = [i for i in declared if not (i.get("collectionGroup") == GROUP and i.get("fields") == target)]
    assert len(without) == len(declared) - 1
    path = tmp_path / "indexes.json"
    path.write_text(json.dumps(without))
    _, repo, _ = _world(EvalDb(indexes=path, groups=_groups()))
    await repo.save_batch([email("m1", 1)])
    with pytest.raises(FailedPrecondition):
        await batch(repo)


async def test_with_no_settled_state_only_the_stale_read_runs(world):
    _, repo, store = world
    await repo.save_batch([email("old_carded", 1), email("young_carded", 9)])
    for email_id in ("old_carded", "young_carded"):
        await card(store, email_id, carded())

    assert ids(await repo.memory_batch("user-1", 10, (), STALE_BEFORE)) == ["old_carded"]


async def test_a_stored_mail_that_does_not_parse_is_left_out(world):
    db, repo, _ = world
    await repo.save_batch([email("good", 2)])
    await db.collection(FACTS).document("user-1_broken").set(
        {"user_id": "user-1", "consolidated_at": None, "indexed_at": T0 + timedelta(hours=1), "card_state": "carded"})

    assert ids(await batch(repo)) == ["good"]
