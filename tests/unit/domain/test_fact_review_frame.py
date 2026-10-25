"""The history pair around a memory-review question (Facts S4 · T1a, plan §3.7, §3.9, §3.10): the exact shapes the builders
render and what the consolidation serializer does with them. The parsers are gone (S4 inward L12): the model is the only
reader of these shapes, so each test renders a frame or a reply and asserts its exact text."""
import pytest

from src.domain.consolidation_serialization import serialize_messages_for_consolidation
from src.domain.fact_review_frame import not_delivered_frame, reply_part, review_frame
from src.domain.llm import Message, MessagePart

QID = "q-7f3a9c"


def _frame(qid):
    return f"[System: memory review q={qid} — Alek asked the owner about a fact he could not confirm]"


def _not_delivered(qid):
    return f"[System: memory review q={qid} not delivered — the owner did not see the question above]"


def _marker(qid):
    return f"[Reply to memory review q={qid}] "


def test_the_frames_and_the_reply_marker_have_the_exact_shapes_the_prompt_names():
    assert review_frame(QID) == "[System: memory review q=q-7f3a9c — Alek asked the owner about a fact he could not confirm]"
    assert not_delivered_frame(QID) == (
        "[System: memory review q=q-7f3a9c not delivered — the owner did not see the question above]"
    )
    assert reply_part(QID, "3 — it's Y now") == "[Reply to memory review q=q-7f3a9c] 3 — it's Y now"


@pytest.mark.parametrize("qid", [QID, "a", "a_b-9", "Q2"])
def test_each_text_carries_its_question_id_once_in_the_place_its_shape_names(qid):
    assert review_frame(qid) == _frame(qid)
    assert not_delivered_frame(qid) == _not_delivered(qid)
    assert reply_part(qid, "2") == _marker(qid) + "2"
    for text in (review_frame(qid), not_delivered_frame(qid), reply_part(qid, "2")):
        assert text.count(f"q={qid}") == 1


def test_the_owners_words_follow_the_marker_exactly_as_written():
    words = "  yes — still here.\n\nSecond paragraph with [brackets] and q=other \n"
    assert reply_part(QID, words) == _marker(QID) + words
    assert reply_part(QID, "") == _marker(QID)


def test_a_reply_that_quotes_a_marker_is_still_one_reply_under_its_own_marker():
    quoted = reply_part("q-other", "no")
    text = reply_part(QID, quoted)
    assert text == _marker(QID) + quoted
    assert text.startswith(_marker(QID)) and text.index(_marker("q-other")) == len(_marker(QID))


def test_the_three_shapes_differ_and_none_is_the_start_of_another():
    shapes = [review_frame(QID), not_delivered_frame(QID), reply_part(QID, "1")]
    for a in shapes:
        for b in shapes:
            if a is not b:
                assert not b.startswith(a), (a, b)
    assert shapes[0].startswith("[System: memory review q=") and shapes[1].startswith("[System: memory review q=")
    assert shapes[2].startswith("[Reply to memory review q=")


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
def test_an_ordinary_note_or_message_has_neither_the_frame_nor_the_reply_shape(text):
    assert text not in (review_frame("abc"), not_delivered_frame("abc"))
    assert not text.startswith(reply_part("abc", ""))


def test_the_reply_marker_starts_the_text():
    text = reply_part(QID, "2")
    assert text.startswith("[Reply to memory review q=")
    assert text.index("[Reply to memory review q=") == 0 and text.count("[Reply to memory review q=") == 1


@pytest.mark.parametrize("bad", ["", "a b", "a]b", "a\nb", "abc\n", "q=1", "x" * 65, "ід"])
def test_a_question_id_that_would_break_the_shape_is_refused_when_writing(bad):
    for write in (review_frame, not_delivered_frame, lambda i: reply_part(i, "2")):
        with pytest.raises(ValueError, match="question id"):
            write(bad)


def test_the_ids_longest_allowed_form_is_rendered_whole():
    longest = "A" * 64
    assert review_frame(longest) == _frame(longest)
    assert not_delivered_frame(longest) == _not_delivered(longest)
    assert reply_part(longest, "2") == _marker(longest) + "2"


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
    assert user_texts == [_frame(QID), _marker(QID) + "3 — it's Y now", _not_delivered("q2")]
