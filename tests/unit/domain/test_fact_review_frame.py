"""The history pair around a memory-review question (Facts S4 · T1a, plan §3.7, §3.9, §3.10): the exact shapes, their
parsers, and what the consolidation serializer does with them."""
import pytest

from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.fact_review_frame import (
    not_delivered_frame,
    parse_frame,
    parse_not_delivered_frame,
    parse_reply_part,
    reply_part,
    review_frame,
)
from src.domain.llm import Message, MessagePart

QID = "q-7f3a9c"


def test_the_frames_and_the_reply_marker_have_the_exact_shapes_the_prompt_names():
    assert review_frame(QID) == "[System: memory review q=q-7f3a9c — Alek asked the owner about a fact he could not confirm]"
    assert not_delivered_frame(QID) == (
        "[System: memory review q=q-7f3a9c not delivered — the owner did not see the question above]"
    )
    assert reply_part(QID, "3 — it's Y now") == "[Reply to memory review q=q-7f3a9c] 3 — it's Y now"


def test_each_text_round_trips_to_its_question_id():
    assert parse_frame(review_frame(QID)) == QID
    assert parse_not_delivered_frame(not_delivered_frame(QID)) == QID
    assert parse_reply_part(reply_part(QID, "2")) == (QID, "2")


def test_the_owners_words_come_back_exactly_as_written():
    words = "  yes — still here.\n\nSecond paragraph with [brackets] and q=other \n"
    assert parse_reply_part(reply_part(QID, words)) == (QID, words)
    assert parse_reply_part(reply_part(QID, "")) == (QID, "")


def test_a_reply_that_quotes_a_marker_is_still_one_reply():
    quoted = reply_part("q-other", "no")
    assert parse_reply_part(reply_part(QID, quoted)) == (QID, quoted)


def test_each_parser_reads_only_its_own_text():
    frame, undelivered, reply = review_frame(QID), not_delivered_frame(QID), reply_part(QID, "1")
    assert parse_frame(undelivered) is None and parse_frame(reply) is None
    assert parse_not_delivered_frame(frame) is None and parse_not_delivered_frame(reply) is None
    assert parse_reply_part(frame) is None and parse_reply_part(undelivered) is None


@pytest.mark.parametrize("text", [
    "[System: phone call with Lelik, 10:00–10:05 (5 min). The note below was posted to the user's chat.]",
    "[System: late answer to something]",
    "[System: memory review q=abc]",
    "[System: memory review q= — Alek asked the owner about a fact he could not confirm]",
    "[System: memory review q=a b — Alek asked the owner about a fact he could not confirm]",
    "note [System: memory review q=abc — Alek asked the owner about a fact he could not confirm]",
    "[System: memory review q=abc — Alek asked the owner about a fact he could not confirm] and more",
    "[Reply to memory review q=abc]",
    "Reply to memory review q=abc: 2",
    "2",
    "",
])
def test_an_ordinary_note_or_message_is_neither_frame_nor_reply(text):
    assert parse_frame(text) is None
    assert parse_not_delivered_frame(text) is None
    assert parse_reply_part(text) is None


def test_a_reply_marker_must_start_the_text():
    assert parse_reply_part(" " + reply_part(QID, "2")) is None
    assert parse_reply_part("ok " + reply_part(QID, "2")) is None


@pytest.mark.parametrize("bad", ["", "a b", "a]b", "a\nb", "abc\n", "q=1", "x" * 65, "ід"])
def test_a_question_id_that_would_break_the_shape_is_refused_when_writing(bad):
    for write in (review_frame, not_delivered_frame, lambda i: reply_part(i, "2")):
        with pytest.raises(ValueError, match="question id"):
            write(bad)


def test_the_ids_longest_allowed_form_round_trips():
    longest = "A" * 64
    assert parse_frame(review_frame(longest)) == longest


def test_the_markers_survive_the_consolidation_serializers_user_part_rendering():
    history = [
        Message(role="user", parts=[MessagePart(text=review_frame(QID))], created_at=1.0),
        Message(role="model", parts=[MessagePart(text="Is this still true?", full_text="Is this still true?")], created_at=2.0),
        Message(role="user", parts=[MessagePart(text=reply_part(QID, "3 — it's Y now"))], created_at=3.0),
        Message(role="model", parts=[MessagePart(text="Noted.")], created_at=4.0),
        Message(role="user", parts=[MessagePart(text=not_delivered_frame("q2"))], created_at=5.0),
    ]
    rendered = serialize_messages_for_consolidation(history)
    user_texts = [m["parts"][0]["text"] for m in rendered if m["role"] == "user"]
    assert [parse_frame(t) for t in user_texts] == [QID, None, None]
    assert [parse_reply_part(t) for t in user_texts] == [None, (QID, "3 — it's Y now"), None]
    assert [parse_not_delivered_frame(t) for t in user_texts] == [None, None, "q2"]
