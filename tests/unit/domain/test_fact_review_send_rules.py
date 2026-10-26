"""Facts S4 · T8d1: the domain's part of sending a question (plan §3.7): where a question may go (`channel_refusal`),
the question's text in history (`question_text`) and the retraction mark (`FactReviewQuestion.retracted`), plus the
store's `mark_retracted` on the review world's evaluating Firestore fake."""
from datetime import timedelta

import pytest

from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.exceptions import QuestionTransitionError
from src.domain.fact_review_frame import question_text
from src.domain.fact_review_language import CANONICAL_LABELS, fact_question_message
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_rules import channel_refusal
from src.domain.notification import NotificationChannel
from src.domain.ui_messages import UIMessage
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import NOW, REF
from tests.support.review_rule_builders import question as build_question

PRIMARY = NotificationChannel(user_id="u1", platform="slack", channel_id="D1", updated_at=NOW)


def binding(companion: bool) -> ChannelBinding:
    return ChannelBinding(channel_id="D1", agent_type="tutor" if companion else "smart", intent="chat", created_by="u1",
                          companion_config=CompanionConfig(window_threshold=20, batch_size=10) if companion else None)


def appended(**over):
    return build_question(QuestionStatus.PENDING, appended_at=NOW - timedelta(minutes=1), **over)


# ------------------------------------------------------------------------------------------------ the channel

def test_a_question_goes_to_the_primary_channel_bound_or_not():
    assert channel_refusal(PRIMARY, None) is None
    assert channel_refusal(PRIMARY, binding(companion=False)) is None


def test_no_primary_channel_refuses_whatever_the_binding():
    assert channel_refusal(None, None) == "no_primary_channel"
    assert channel_refusal(None, binding(companion=True)) == "no_primary_channel"


def test_a_primary_channel_with_a_companions_memory_refuses():
    assert channel_refusal(PRIMARY, binding(companion=True)) == "companion_channel"


# ------------------------------------------------------------------------------------------------ the question's text

def test_the_question_text_is_the_line_the_quoted_fact_and_the_numbered_options():
    question = build_question(quoted_text="User trains at the Iron Temple gym.")
    message = fact_question_message(question, CANONICAL_LABELS)

    assert question_text(message) == (
        "Is this still true?\n«User trains at the Iron Temple gym.»\n1 — Yes  2 — No longer  3 — Never  4 — Don't remember"
    )
    assert CANONICAL_LABELS[UIMessage.MEMORY_REPLY_HINT.value] not in question_text(message)   # the hint is the owner's, not history's


# ------------------------------------------------------------------------------------------------ the retraction mark

def test_retracting_clears_the_frame_so_no_second_retraction_is_due():
    question = appended()
    assert question.retraction_due

    retracted = question.retracted()

    assert retracted.status is QuestionStatus.PENDING and retracted.appended_at is None
    assert retracted.session_id == question.session_id and retracted.queue_key == question.queue_key
    assert not retracted.retraction_due
    with pytest.raises(QuestionTransitionError):
        retracted.retracted()


def test_a_retracted_question_cannot_be_posted_and_goes_back_to_the_queue():
    retracted = appended().retracted()

    with pytest.raises(QuestionTransitionError):
        retracted.posted(REF)
    assert retracted.requeued().status is QuestionStatus.QUEUED


@pytest.mark.parametrize("make", [
    lambda: build_question(QuestionStatus.PENDING, appended_at=None),                 # nothing appended
    lambda: build_question(QuestionStatus.PENDING, posted=True),                      # delivered
    lambda: build_question(QuestionStatus.QUEUED),
    lambda: build_question(QuestionStatus.ASKED),
    lambda: appended().closed(ClosedBy.RESOLVED_ELSEWHERE, NOW),
])
def test_only_a_question_due_for_a_retraction_is_retracted(make):
    with pytest.raises(QuestionTransitionError):
        make().retracted()


async def test_the_store_marks_a_retraction_once():
    world = ReviewWorld()
    await world.put_question(appended())

    assert await world.review_store.mark_retracted("q1")
    stored = (await world.review_store.get_questions(["q1"]))[0]
    assert stored.status is QuestionStatus.PENDING and stored.appended_at is None
    raw = world.question_docs["q1"]                  # the stored document itself (reviewer, T8d1 review)
    assert raw["status"] == "pending" and raw["appended_at"] is None and raw["session_id"] == stored.session_id
    assert not await world.review_store.mark_retracted("q1")
    assert not await world.review_store.mark_retracted("missing")
