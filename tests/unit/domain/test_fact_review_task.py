"""The review pass's input as a domain shape and its one plain-data form (Facts S4 T6c, plan §3.4)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain.entities import ExpiryKind, FactEntity, FactState, FactType
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_task import (
    MAX_OWNER_MESSAGES,
    MAX_OWNER_MESSAGES_CHARS,
    PAYLOAD_KEY,
    FactReviewTask,
    ReviewItem,
    decode_review_task,
    encode_review_task,
    owner_messages_block,
)
from tests.support.review_rule_builders import question

UTC = timezone.utc
AT = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)


def fact(fact_id, **over):
    return FactEntity(id=fact_id, account_id="acc", created_by_user_id="u1", lineage_id=fact_id, text=f"text {fact_id}",
                      type=FactType.STATE, reported_date=AT, **over)


def task(**over):
    stale = fact("f1", state=FactState.STALE, stale_since=AT + timedelta(days=30), expiry_kind=ExpiryKind.HORIZON,
                 ttl_days=14, expires_at=AT + timedelta(days=14),
                 lifecycle_events=[{"at": AT + timedelta(days=30), "from": "current", "to": "stale", "actor": "janitor"}])
    fields = dict(
        items=(ReviewItem(fact=stale, versions=(fact("f0", version=1),), neighbours=(fact("n1"),)),),
        questions=(question(QuestionStatus.ASKED, fact_id="f1"),),
        mirror=True,
        owner_messages=("hola", "¿qué tal?"),
    )
    fields.update(over)
    return FactReviewTask(**fields)


def test_the_plain_data_form_reads_back_to_an_equal_task():
    original = task()
    payload = encode_review_task(original)
    assert set(payload) == {PAYLOAD_KEY}
    assert decode_review_task({"task": "review_stale_facts", **payload}) == original


def test_the_plain_data_form_holds_no_domain_object():
    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                yield from walk(v)
        elif isinstance(value, (list, tuple)):
            for v in value:
                yield from walk(v)
        else:
            yield value
    leaves = list(walk(encode_review_task(task())))
    assert all(not hasattr(leaf, "model_dump") for leaf in leaves)


def test_the_event_times_of_a_batch_fact_survive_the_form():
    decoded = decode_review_task(encode_review_task(task()))
    assert decoded.items[0].fact.lifecycle_events[0]["at"] == AT + timedelta(days=30)


@pytest.mark.parametrize("payload", [{}, {PAYLOAD_KEY: None}, {PAYLOAD_KEY: "x"}, {PAYLOAD_KEY: {"items": []}},
                                     {PAYLOAD_KEY: {"items": [], "mirror": False}}])
def test_a_payload_without_a_valid_task_is_refused(payload):
    with pytest.raises(ValueError):
        decode_review_task(payload)


def test_the_batch_is_the_items_facts_and_the_questions():
    t = task()
    assert [f.id for f in t.batch.facts] == ["f1"] and t.batch.questions == t.questions


def test_the_question_of_a_fact_is_its_open_one():
    closed = question(QuestionStatus.CLOSED, question_id="q2", fact_id="f1")
    t = task(questions=(closed,))
    assert t.question_for("f1") is None
    t = task(questions=(closed, question(QuestionStatus.ASKED, question_id="q3", fact_id="f1")))
    assert t.question_for("f1").question_id == "q3" and t.question_for("n1") is None


def test_owner_messages_belong_to_mirror_mode():
    with pytest.raises(ValueError):
        task(mirror=False)
    assert task(mirror=False, owner_messages=()).owner_messages == ()


def test_the_block_drops_system_frames_and_empty_texts():
    assert owner_messages_block(["a", "[System: reminder]", "  [System: x]", " ", "b"]) == ("a", "b")


def test_the_block_keeps_the_latest_messages():
    texts = [f"m{i}" for i in range(MAX_OWNER_MESSAGES + 3)]
    assert owner_messages_block(texts) == tuple(texts[3:])


def test_the_block_fits_the_character_bound_and_keeps_the_newest():
    texts = ["x" * 1000, "y" * 800, "z" * 600]
    block = owner_messages_block(texts)
    assert sum(len(t) for t in block) == MAX_OWNER_MESSAGES_CHARS
    assert block == ("x" * (MAX_OWNER_MESSAGES_CHARS - 1400), "y" * 800, "z" * 600)   # the oldest kept is cut


def test_a_task_holds_the_block():
    t = task(owner_messages=("[System: x]", "hola"))
    assert t.owner_messages == ("hola",)
