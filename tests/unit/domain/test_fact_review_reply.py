"""The paths of an owner's reply to a memory-review question and the text that answers each (Facts S4 · T8f, plan §3.16)."""
import pytest

from src.domain.fact_answer_result import AnswerResult
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import QuestionStatus
from src.domain.fact_review_reply import ReplyPath, acknowledgement, needs_completing, reply_path
from src.domain.ui_messages import UIMessage
from tests.support.review_rule_builders import question


class TestReplyPath:
    @pytest.mark.parametrize("status", [QuestionStatus.ASKED, QuestionStatus.UNANSWERED])
    def test_a_digit_on_a_sent_question_is_applied_and_words_are_recorded(self, status):
        assert reply_path(question(status), AnswerKind.ENDED) is ReplyPath.DIGIT
        assert reply_path(question(status), None) is ReplyPath.WORDS

    def test_a_delivered_pending_question_takes_replies_like_an_asked_one(self):
        pending = question(QuestionStatus.PENDING, posted=True)
        assert reply_path(pending, AnswerKind.NEVER) is ReplyPath.DIGIT
        assert reply_path(pending, None) is ReplyPath.WORDS

    @pytest.mark.parametrize("digit", [None, AnswerKind.STILL_TRUE])
    def test_a_closed_question_is_already_answered_whatever_the_reply(self, digit):
        assert reply_path(question(QuestionStatus.CLOSED), digit) is ReplyPath.ALREADY_ANSWERED

    @pytest.mark.parametrize("digit", [None, AnswerKind.STILL_TRUE])
    def test_an_open_question_that_was_never_delivered_counts_for_nothing(self, digit):
        assert reply_path(question(QuestionStatus.PENDING), digit) is None
        assert reply_path(question(QuestionStatus.QUEUED), digit) is None


class TestNeedsCompleting:
    def test_only_a_delivered_pending_question_is(self):
        assert needs_completing(question(QuestionStatus.PENDING, posted=True))
        assert not needs_completing(question(QuestionStatus.PENDING))
        for status in (QuestionStatus.QUEUED, QuestionStatus.ASKED, QuestionStatus.UNANSWERED, QuestionStatus.CLOSED):
            assert not needs_completing(question(status))


class TestAcknowledgement:
    def test_words_and_a_closed_question_have_their_own_text(self):
        assert acknowledgement(ReplyPath.WORDS) is UIMessage.MEMORY_REPLY_NOTED
        assert acknowledgement(ReplyPath.ALREADY_ANSWERED) is UIMessage.MEMORY_ALREADY_ANSWERED

    def test_a_digit_is_noted_when_applied_and_already_answered_when_the_question_closed_meanwhile(self):
        assert acknowledgement(ReplyPath.DIGIT, AnswerResult.APPLIED) is UIMessage.MEMORY_ANSWER_NOTED
        assert acknowledgement(ReplyPath.DIGIT, AnswerResult.NOT_OPEN) is UIMessage.MEMORY_ALREADY_ANSWERED

    @pytest.mark.parametrize("result", [AnswerResult.CHANGED, AnswerResult.CONFLICT])
    def test_a_digit_on_a_fact_that_changed_or_whose_write_conflicted_says_so(self, result):
        assert acknowledgement(ReplyPath.DIGIT, result) is UIMessage.MEMORY_FACT_CHANGED

    @pytest.mark.parametrize("result", [AnswerResult.NOT_FOUND, AnswerResult.NOT_APPLIED, None])
    def test_a_digit_that_is_not_applied_for_another_reason_is_not_answered_here(self, result):
        assert acknowledgement(ReplyPath.DIGIT, result) is None
