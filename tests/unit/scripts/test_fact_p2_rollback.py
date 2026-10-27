"""The Facts P2 rollback script: question documents back to the S4 shape, on the evaluating fake."""
import copy
import json
from datetime import datetime, timezone

import pytest

from scripts.migration import fact_p2_rollback_questions as rollback
from src.domain.fact_review_question import QuestionStatus
from tests.support.fact_review_world import ReviewWorld
from tests.support.firestore_eval import EvalDb, EvalDocRef
from tests.support.review_rule_builders import question as build_question

NOW = datetime(2026, 10, 28, 9, 30, tzinfo=timezone.utc)
PURGE = datetime(2027, 1, 20, tzinfo=timezone.utc)
COL = "eval_questions"

# Frozen: the S4 FactReviewQuestion fields (today's model_fields minus question, texts, origin, delivery, fallback_attempts),
# and the fields the store adds beside the model.
S4_FIELDS = frozenset({
    "question_id", "account_id", "user_id", "fact_id", "quoted_text", "fallback", "fallback_date", "noise", "status",
    "queue_key", "queued_at", "session_id", "appended_at", "asked_at", "message_ref", "reply_text", "reply_at",
    "offered_at", "answer", "answer_source", "closed_at", "closed_by",
})
STORED_ONLY = frozenset({"message_platform", "message_channel", "message_id", "purge_after", "reply_not_shown"})
S4_ANSWERS = {"still_true", "ended", "never", "dont_know", None}

TEXTS = {"happened": "Alex gave the talk", "not_happened": "Alex did not give the talk", "unknown": "unknown"}
PAID = {"paid": "Alex paid the parking fine", "not_paid": "payment not confirmed"}


def _doc(**over):
    """A stored question in the P2 shape: the five P2 keys at their defaults unless `over` says more."""
    doc = {
        "question_id": "q", "account_id": "acc", "user_id": "u", "fact_id": "f", "quoted_text": "Drinks green tea",
        "fallback": "revive", "fallback_date": None, "noise": False, "status": "queued", "queue_key": NOW,
        "queued_at": NOW, "question": "still_true", "texts": {}, "origin": "review", "delivery": "chat",
        "fallback_attempts": 0,
    }
    doc.update(over)
    return doc


CLOSED = dict(status="closed", closed_at=NOW, closed_by="owner", answer="happened", answer_source="cabinet",
              question="happened", texts=TEXTS, fallback="history", purge_after=PURGE, asked_at=NOW)


@pytest.fixture
async def db():
    fake = EvalDb()
    col = fake.collection(COL)
    await col.document("default-only").set(_doc())
    await col.document("open-happened").set(_doc(question="happened", texts=TEXTS, fallback="history", status="asked",
                                                  asked_at=NOW, fallback_attempts=1))
    await col.document("open-kept-twice").set(_doc(origin="kept_twice", fallback="archive", noise=True,
                                                    fallback_date="2026-11-01", status="queued"))
    await col.document("closed-happened").set(_doc(**CLOSED))
    await col.document("digest").set(_doc(question="paid", delivery="digest", texts=PAID, fallback="history"))
    await col.document("s4-old").set({k: v for k, v in _doc().items() if k in S4_FIELDS})
    return fake


def _snapshot(db):
    col = db.collection(COL)
    return copy.deepcopy(col.docs), dict(col.versions)


def test_the_s4_field_set_is_todays_model_minus_the_five_p2_fields():
    from src.domain.fact_review_question import FactReviewQuestion

    assert set(FactReviewQuestion.model_fields) - set(rollback.P2_KEYS) == S4_FIELDS


@pytest.mark.parametrize("doc, p2", [
    (_doc(), False),
    (_doc(question="paid"), True),
    (_doc(texts={"a": "b"}), True),
    (_doc(origin="kept_twice"), True),
    (_doc(delivery="digest"), True),
    (_doc(fallback_attempts=2), True),
    (_doc(fallback="history"), True),
    (_doc(answer="paid"), True),
    (_doc(answer="ended"), False),
    ({"fallback": "revive"}, False),
])
def test_what_makes_a_record_p2(doc, p2):
    assert rollback.is_p2_record(doc) is p2


async def test_dry_run_writes_nothing_and_reports_ids_without_text(db, tmp_path, capsys):
    before = _snapshot(db)

    counts = await rollback.run(db, COL, apply=False, out_dir=tmp_path, now=NOW)

    assert _snapshot(db) == before
    assert counts == {"p2_records": 4, "p2_defaults_only": 1, "deleted": 0, "rewritten": 0, "stripped": 0,
                      "skipped_changed": 0, "failed": 0}
    raw = (tmp_path / "fact_p2_rollback_2026-10-28.json").read_text(encoding="utf-8")
    plans = json.loads(raw)["plans"]
    assert plans["digest"] == {"p2_record": True, "action": "deleted"}
    assert plans["default-only"] == {"p2_record": False, "action": "stripped"}
    assert "s4-old" not in plans
    assert "green tea" not in raw and "parking fine" not in raw
    out = capsys.readouterr().out
    assert "p2_records: 4" in out and "digest" not in out


async def test_apply_leaves_every_document_in_the_s4_shape(db, tmp_path):
    counts = await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs = db.collection(COL).docs
    assert counts["deleted"] == 1 and counts["rewritten"] == 3 and counts["stripped"] == 1 and counts["failed"] == 0
    assert "digest" not in docs
    for doc_id, doc in docs.items():
        assert set(doc) <= S4_FIELDS | STORED_ONLY, doc_id
        assert doc["fallback"] in ("revive", "archive"), doc_id
        assert doc.get("answer") in S4_ANSWERS, doc_id


async def test_a_closed_p2_record_is_resolved_elsewhere_and_keeps_its_purge_stamp(db, tmp_path):
    await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    closed = db.collection(COL).docs["closed-happened"]
    assert (closed["closed_by"], closed["answer"], closed["answer_source"]) == ("resolved_elsewhere", None, None)
    assert closed["purge_after"] == PURGE and closed["status"] == "closed" and closed["closed_at"] == NOW


async def test_an_open_p2_record_falls_back_to_a_revive(db, tmp_path):
    await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    docs = db.collection(COL).docs
    for doc_id in ("open-happened", "open-kept-twice"):
        assert (docs[doc_id]["fallback"], docs[doc_id]["noise"], docs[doc_id]["fallback_date"]) == ("revive", False, None)
    assert docs["open-happened"]["status"] == "asked" and docs["open-happened"]["quoted_text"] == "Drinks green tea"


async def test_a_default_only_record_is_only_stripped(db, tmp_path):
    before = _snapshot(db)[0]["default-only"]

    await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    after = db.collection(COL).docs["default-only"]
    assert after == {k: v for k, v in before.items() if k not in rollback.P2_KEYS}


async def test_a_document_already_in_the_s4_shape_is_not_touched(db, tmp_path):
    before_docs, before_versions = _snapshot(db)

    await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert db.collection(COL).docs["s4-old"] == before_docs["s4-old"]
    assert db.collection(COL).versions["s4-old"] == before_versions["s4-old"]


async def test_a_second_apply_does_nothing(db, tmp_path):
    await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)
    before = _snapshot(db)

    counts = await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts == {"p2_records": 0, "p2_defaults_only": 0, "deleted": 0, "rewritten": 0, "stripped": 0,
                      "skipped_changed": 0, "failed": 0}
    assert _snapshot(db) == before


async def test_a_changed_or_deleted_document_is_skipped(db, tmp_path, monkeypatch):
    real_scan = rollback.scan

    async def scan_then_interfere(database, collection):
        result = await real_scan(database, collection)
        await database.collection(COL).document("open-happened").update({"reply_text": "edited meanwhile"})
        await database.collection(COL).document("digest").delete()
        return result

    monkeypatch.setattr(rollback, "scan", scan_then_interfere)

    counts = await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["skipped_changed"] == 2 and counts["rewritten"] == 2 and counts["stripped"] == 1
    assert db.collection(COL).docs["open-happened"]["question"] == "happened"


async def test_a_general_write_error_counts_as_failed(db, tmp_path, monkeypatch, capsys):
    real_update = EvalDocRef.update

    async def update(self, data, option=None):
        if self.id == "default-only":
            raise RuntimeError("Drinks green tea")
        return await real_update(self, data, option)

    monkeypatch.setattr(EvalDocRef, "update", update)

    counts = await rollback.run(db, COL, apply=True, out_dir=tmp_path, now=NOW)

    assert counts["failed"] == 1
    out = capsys.readouterr().out
    assert "RuntimeError" in out and "green tea" not in out
    assert "question" in db.collection(COL).docs["default-only"]


def _main_with_fake_client(monkeypatch, tmp_path, argv):
    import scripts._firestore as client_helper
    from src.config.environment import EnvironmentConfig

    collection = EnvironmentConfig().fact_review_questions_collection
    fake = EvalDb()
    for name in (collection, "some_other_collection"):
        fake.collection(name).docs["default-only"] = _doc()
        fake.collection(name).versions["default-only"] = 1
    monkeypatch.setattr(client_helper, "async_client", lambda: fake)
    monkeypatch.setattr(rollback, "MEMORY_DIR", tmp_path)
    assert rollback.main(argv) == 0
    return fake, collection


def test_main_is_a_dry_run_by_default_on_the_questions_collection(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, [])

    assert "question" in fake.collection(collection).docs["default-only"]
    assert list(tmp_path.glob("fact_p2_rollback_*.json")) and not list(tmp_path.glob("*_applied.json"))


def test_main_apply_strips_the_questions_collection_only(monkeypatch, tmp_path):
    fake, collection = _main_with_fake_client(monkeypatch, tmp_path, ["--apply"])

    assert "question" not in fake.collection(collection).docs["default-only"]
    assert "question" in fake.collection("some_other_collection").docs["default-only"]


async def test_the_p2_models_own_documents_roll_back_to_a_shape_the_s4_field_set_holds(tmp_path):
    """Real P2 documents (`question_document` of the model) go through the script, not hand-made ones."""
    world = ReviewWorld()
    await world.put_question(build_question(
        QuestionStatus.ASKED, question_id="q-talk", fact_id="f-talk", question="happened", fallback="history", texts=TEXTS))
    await world.put_question(build_question(QuestionStatus.QUEUED, question_id="q-plain", fact_id="f-plain"))
    docs = world.question_docs
    assert "texts" in docs["q-talk"] and "question" in docs["q-plain"]   # the model stores the defaults too

    await rollback.run(world.store.db, world.env.fact_review_questions_collection, apply=True, out_dir=tmp_path, now=NOW)

    for doc in docs.values():
        assert set(doc) <= S4_FIELDS | STORED_ONLY and doc["fallback"] in ("revive", "archive")
