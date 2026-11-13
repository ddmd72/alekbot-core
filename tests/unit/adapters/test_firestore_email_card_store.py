"""FirestoreEmailCardStore (slice 6): a card write is a compare-and-set field update on the v1 document. Over the
evaluating fake, with the v1 document written by the real repository: no v1 field changes, the vector is stored as a
vector, a stale expectation conflicts, a missing document is never created, v1 reads are unchanged."""
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_email_card_store import FirestoreEmailCardStore
from src.adapters.firestore_indexed_email_repo import FirestoreIndexedEmailRepository
from src.domain.email import IndexedEmail
from src.domain.email_card_answer import parse_mail_answer
from src.domain.email_card_build import build_card
from src.domain.email_card_input import build_card_input
from src.domain.email_card_lifecycle import CardExpect, CardWriteResult, pending_fields, settled_fields
from tests.support.firestore_eval import EvalDb, eval_transactions, stored_vector
from tests.unit.domain.email_card_fixtures import BOOKING_BODY, NONCE, PEOPLE, answer_json, mail

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
FACTS, STATE = "eval_email_facts", "eval_email_state"
D0 = datetime(2026, 10, 5, 10, 0)
NOW = datetime(2026, 11, 13, 8, 0, tzinfo=timezone.utc)
V1_FIELDS = ("text", "vector", "tags_vector", "state", "indexed_at", "consolidated_at", "subject", "email_date")


@pytest.fixture
def world():
    db = EvalDb(indexes=INDEXES_FILE, groups={FACTS: "development_domain_email_facts_v1",
                                               STATE: "development_email_indexing_state"})
    env = SimpleNamespace(domain_email_facts_collection=FACTS, email_indexing_state_collection=STATE)
    with eval_transactions():
        yield db, FirestoreIndexedEmailRepository(db, env), FirestoreEmailCardStore(db, env)


def email(email_id, user_id="user-1", **over) -> IndexedEmail:
    fields = dict(email_id=email_id, user_id=user_id, account_id="acct-1", source="gmail",
                  text="Hotel booking RM-55120 confirmed.", vector=[1.0, 0.0, 0.0], tags_vector=[0.0, 1.0, 0.0],
                  tags=["hotel"], category="travel", metadata={"subject": "Booking"}, subject="Booking",
                  from_address="booking@albergo.example", email_date=D0, indexed_at=D0)
    fields.update(over)
    return IndexedEmail(**fields)


def card_fields():
    m = mail()
    inp = build_card_input(m, BOOKING_BODY, [], [], direct=True, nonce=NONCE)
    card = build_card(parse_mail_answer(answer_json()), m, [], inp.evidence, PEOPLE)
    return settled_fields(card, digest="dg", model="flash-lite", now=NOW, vector_pending=False)


async def raw(db, email_id="m1"):
    snap = await db.collection(FACTS).document(f"user-1_{email_id}").get()
    return snap.to_dict()


async def test_a_card_write_updates_card_fields_and_no_v1_field(world):
    db, repo, store = world
    await repo.save_batch([email("m1", consolidated_at=D0)])
    before = await raw(db)

    result = await store.write_card("user-1", "m1", card_fields(), [0.5, 0.5, 0.0], CardExpect.absent())

    after = await raw(db)
    assert result is CardWriteResult.WRITTEN
    for name in V1_FIELDS:
        assert after[name] == before[name]
    assert after["card_state"] == "carded" and after["people_digest"] == "dg" and after["about_keys"]
    assert stored_vector(after["card_vector"]) == [0.5, 0.5, 0.0]
    assert set(after) - set(before) <= set(card_fields()) | {"card_vector"}


async def test_a_second_absent_write_conflicts_and_changes_nothing(world):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    await store.write_card("user-1", "m1", card_fields(), [0.5, 0.5, 0.0], CardExpect.absent())
    snapshot = await raw(db)

    result = await store.write_card("user-1", "m1", pending_fields(reason="outage", failures=0, model=""), None,
                                    CardExpect.absent())

    assert result is CardWriteResult.CONFLICT
    assert await raw(db) == snapshot


async def test_the_stored_pair_as_expectation_writes_and_another_digest_conflicts(world):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    fields = card_fields()
    await store.write_card("user-1", "m1", fields, None, CardExpect.absent())

    stale = await store.write_card("user-1", "m1", fields, None, CardExpect(fields["card_version"], "other"))
    fresh = await store.write_card("user-1", "m1", pending_fields(reason="budget", failures=0, model=""), None,
                                   CardExpect(fields["card_version"], "dg"))

    assert (stale, fresh) == (CardWriteResult.CONFLICT, CardWriteResult.WRITTEN)
    assert (await raw(db))["card_state"] == "pending"


async def test_a_missing_document_is_reported_and_never_created(world):
    db, _, store = world
    result = await store.write_card("user-1", "m404", card_fields(), None, CardExpect.absent())
    assert result is CardWriteResult.MISSING
    assert await raw(db, "m404") is None


async def test_a_change_inside_the_transaction_is_retried_then_conflicts(world):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    fields = card_fields()
    db.interfere_on_txn_read(FACTS, "user-1_m1", {"card_version": 1, "people_digest": "x"}, after_read=True)

    result = await store.write_card("user-1", "m1", fields, None, CardExpect.absent())

    assert result is CardWriteResult.CONFLICT
    assert (await raw(db))["people_digest"] == "x"


async def test_a_key_outside_the_card_fields_is_refused_before_any_write(world):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    before = await raw(db)
    for bad in ({"text": "x"}, {"consolidated_at": None}, {"card_state": "carded", "state": "archived"},
                {"card_vector": [1.0]}):
        with pytest.raises(ValueError):
            await store.write_card("user-1", "m1", bad, None, CardExpect.absent())
    assert await raw(db) == before


async def test_no_vector_deletes_a_stored_card_vector(world):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    fields = card_fields()
    await store.write_card("user-1", "m1", fields, [0.5, 0.5, 0.0], CardExpect.absent())
    assert "card_vector" in await raw(db)

    await store.write_card("user-1", "m1", fields, None, CardExpect(fields["card_version"], "dg"))

    assert "card_vector" not in await raw(db)


async def test_read_expect_maps_present_documents_and_leaves_absent_ones_out(world):
    _, repo, store = world
    await repo.save_batch([email("m1"), email("m2"), email("m9", user_id="user-2")])
    fields = card_fields()
    await store.write_card("user-1", "m1", fields, None, CardExpect.absent())

    got = await store.read_expect("user-1", ["m1", "m2", "m3", "m9"])

    assert got == {"m1": CardExpect(fields["card_version"], "dg"), "m2": CardExpect.absent()}


async def test_read_expect_chunks_501_ids_into_two_calls(world, monkeypatch):
    db, repo, store = world
    await repo.save_batch([email("m1")])
    sizes = []
    original = db.get_all

    def spy(refs, field_paths=None):
        sizes.append(len(refs))
        return original(refs, field_paths=field_paths)

    monkeypatch.setattr(db, "get_all", spy)
    got = await store.read_expect("user-1", [f"m{i}" for i in range(1, 502)])

    assert sizes == [500, 1] and list(got) == ["m1"]


async def test_v1_reads_are_unchanged_after_a_card_write(world):
    _, repo, store = world
    await repo.save_batch([email("m1"), email("m2", consolidated_at=D0)])
    batch_before = [e.model_dump() for e in await repo.get_unconsolidated_batch("user-1", 10)]
    marks_before = await repo.get_consolidation_marks("user-1", ["m1", "m2"])

    await store.write_card("user-1", "m1", card_fields(), [0.5, 0.5, 0.0], CardExpect.absent())
    await store.write_card("user-1", "m2", pending_fields(reason="error", failures=0, model=""), None,
                           CardExpect.absent())

    assert [e.model_dump() for e in await repo.get_unconsolidated_batch("user-1", 10)] == batch_before
    assert await repo.get_consolidation_marks("user-1", ["m1", "m2"]) == marks_before
