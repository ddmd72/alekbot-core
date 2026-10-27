"""A memory-review question: its fields, its life and the moves that make it (Facts S4 · T1a, plan §2.2, §3.1, §3.7-§3.9)."""
from datetime import date, datetime, timedelta, timezone
from itertools import product

import pytest
from pydantic import ValidationError

from src.adapters.firestore_fact_review_store import question_document, question_from
from src.domain.exceptions import QuestionTransitionError
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_question import (
    MAX_REPLY_CHARS,
    QUESTION_RETENTION,
    QUESTION_TIMEOUT,
    ClosedBy,
    FactReviewQuestion,
    QuestionStatus,
)
from src.domain.messaging import MessageRef

UTC = timezone.utc
NOW = datetime(2026, 10, 24, 9, 0, tzinfo=UTC)
T_APPEND = NOW - timedelta(days=3, minutes=2)
T_ASKED = NOW - timedelta(days=3)
REF = MessageRef(platform="slack", channel_id="D1", message_id="1729760000.000100")
Q = QuestionStatus


def make(**over):
    """A question built straight from fields: the tests of the invariants break one rule at a time."""
    fields = dict(
        question_id="q1", account_id="acc", user_id="u1", fact_id="f1", quoted_text="Lives in Lisbon",
        fallback="revive", queue_key=NOW - timedelta(days=30), queued_at=NOW - timedelta(days=4),
    )
    fields.update(over)
    return FactReviewQuestion(**fields)


SENT = dict(session_id="u1:D1", appended_at=T_APPEND, message_ref=REF, asked_at=T_ASKED)

STATES = {
    "queued": lambda: make(),
    "pending_new": lambda: make(status=Q.PENDING, session_id="u1:D1"),
    "pending_appended": lambda: make(status=Q.PENDING, session_id="u1:D1", appended_at=T_APPEND),
    "pending_posted": lambda: make(status=Q.PENDING, session_id="u1:D1", appended_at=T_APPEND, message_ref=REF),
    "asked": lambda: make(status=Q.ASKED, **SENT),
    "asked_replied": lambda: make(status=Q.ASKED, reply_text="it is Y now", reply_at=NOW - timedelta(days=1), **SENT),
    "asked_offered": lambda: make(
        status=Q.ASKED, reply_text="it is Y now", reply_at=NOW - timedelta(days=1),
        offered_at=NOW - timedelta(hours=1), **SENT,
    ),
    "unanswered": lambda: make(status=Q.UNANSWERED, **SENT),
    "closed": lambda: make(status=Q.ASKED, **SENT).closed(ClosedBy.TIMEOUT, NOW),
}

MOVES = {
    "claimed": lambda q: q.claimed("u1:D1"),
    "appended": lambda q: q.appended(T_APPEND),
    "posted": lambda q: q.posted(REF),
    "asked": lambda q: q.asked(T_ASKED),
    "completed": lambda q: q.completed(),
    "requeued": lambda q: q.requeued(),
    "unanswered": lambda q: q.unanswered(),
    "with_reply": lambda q: q.with_reply("words", NOW),
    "offered": lambda q: q.offered(NOW),
    "closed": lambda q: q.closed(ClosedBy.RESOLVED_ELSEWHERE, NOW),
}

# (state, move) -> the status the move leads to; every other pair is refused.
ALLOWED = {
    ("queued", "claimed"): Q.PENDING, ("queued", "closed"): Q.CLOSED,
    ("pending_new", "appended"): Q.PENDING,
    ("pending_new", "requeued"): Q.QUEUED, ("pending_new", "closed"): Q.CLOSED,
    ("pending_appended", "posted"): Q.PENDING, ("pending_appended", "requeued"): Q.QUEUED,
    ("pending_appended", "closed"): Q.CLOSED,
    ("pending_posted", "asked"): Q.ASKED, ("pending_posted", "completed"): Q.ASKED,
    ("pending_posted", "closed"): Q.CLOSED,
    ("asked", "unanswered"): Q.UNANSWERED, ("asked", "with_reply"): Q.ASKED, ("asked", "closed"): Q.CLOSED,
    ("asked_replied", "with_reply"): Q.ASKED, ("asked_replied", "offered"): Q.ASKED,
    ("asked_replied", "unanswered"): Q.UNANSWERED, ("asked_replied", "closed"): Q.CLOSED,
    ("asked_offered", "unanswered"): Q.UNANSWERED, ("asked_offered", "closed"): Q.CLOSED,
    ("unanswered", "with_reply"): Q.UNANSWERED, ("unanswered", "closed"): Q.CLOSED,
}


# ------------------------------------------------------------------------------------------------ the moves, as a table

@pytest.mark.parametrize("state,move", list(product(STATES, MOVES)))
def test_every_move_from_every_state_is_allowed_or_refused_as_the_table_says(state, move):
    question = STATES[state]()
    if (state, move) in ALLOWED:
        moved = MOVES[move](question)
        assert moved.status is ALLOWED[(state, move)]
        assert moved is not question and moved.question_id == question.question_id
    else:
        with pytest.raises(QuestionTransitionError):
            MOVES[move](question)


@pytest.mark.parametrize("state,move", list(product(STATES, MOVES)))
def test_a_refused_move_leaves_the_question_as_it_was(state, move):
    question = STATES[state]()
    before = question.model_dump()
    try:
        MOVES[move](question)
    except QuestionTransitionError:
        pass
    assert question.model_dump() == before


def test_a_question_that_walks_its_whole_life_ends_closed_with_everything_it_gathered():
    q = make(fallback="archive", fallback_date=date(2026, 9, 1), noise=True)
    q = q.claimed("u1:D1").appended(T_APPEND).posted(REF).asked(T_ASKED).unanswered()
    q = q.with_reply("it is Y now", NOW - timedelta(days=1)).offered(NOW - timedelta(hours=1))
    q = q.closed(ClosedBy.OWNER, NOW, AnswerKind.ENDED, "button")
    assert (q.status, q.session_id, q.appended_at, q.message_ref, q.asked_at) == (Q.CLOSED, "u1:D1", T_APPEND, REF, T_ASKED)
    assert (q.reply_text, q.answer, q.answer_source, q.closed_by) == ("it is Y now", AnswerKind.ENDED, "button", ClosedBy.OWNER)
    assert (q.fallback, q.fallback_date, q.noise, q.quoted_text) == ("archive", date(2026, 9, 1), True, "Lives in Lisbon")


# ------------------------------------------------------------------------------------------------ each move

def test_claimed_names_the_session_the_question_will_be_sent_in():
    claimed = make().claimed("u1:D1")
    assert (claimed.status, claimed.session_id) == (Q.PENDING, "u1:D1")
    with pytest.raises(QuestionTransitionError):
        make().claimed("")


def test_appended_and_posted_record_when_and_where():
    q = STATES["pending_new"]().appended(T_APPEND).posted(REF)
    assert (q.appended_at, q.message_ref, q.status) == (T_APPEND, REF, Q.PENDING)


def test_asked_stamps_the_time_and_keeps_the_message():
    q = STATES["pending_posted"]().asked(T_ASKED)
    assert (q.status, q.asked_at, q.message_ref, q.appended_at) == (Q.ASKED, T_ASKED, REF, T_APPEND)


def test_a_question_cannot_be_posted_before_its_frame_is_appended():
    pending = STATES["pending_new"]()
    with pytest.raises(QuestionTransitionError, match="cannot be posted"):
        pending.posted(REF)
    assert pending.appended(T_APPEND).posted(REF).delivered


def test_completed_is_asked_at_the_latest_time_known_before_the_post():
    q = STATES["pending_posted"]().completed()
    assert (q.status, q.asked_at, q.message_ref) == (Q.ASKED, T_APPEND, REF)


def test_requeued_forgets_the_attempt_and_keeps_the_place_in_the_queue():
    pending = STATES["pending_appended"]()
    back = pending.requeued()
    assert back.status is Q.QUEUED
    assert (back.session_id, back.appended_at, back.message_ref, back.asked_at) == (None, None, None, None)
    assert (back.queue_key, back.queued_at, back.fact_id, back.quoted_text) == (
        pending.queue_key, pending.queued_at, pending.fact_id, pending.quoted_text)
    assert back.claimed("u1:D1").appended_at is None


def test_unanswered_keeps_the_ask_time_and_so_the_timeout():
    q = STATES["asked"]().unanswered()
    assert (q.status, q.asked_at) == (Q.UNANSWERED, T_ASKED)


def test_a_naive_time_in_a_move_is_refused_with_the_domain_error():
    naive = datetime(2026, 10, 24, 9, 0)
    with pytest.raises(QuestionTransitionError):
        STATES["pending_new"]().appended(naive)
    with pytest.raises(QuestionTransitionError):
        STATES["pending_posted"]().asked(naive)
    with pytest.raises(QuestionTransitionError):
        STATES["asked"]().with_reply("x", naive)
    with pytest.raises(QuestionTransitionError):
        STATES["asked_replied"]().offered(naive)
    with pytest.raises(QuestionTransitionError):
        STATES["asked"]().closed(ClosedBy.TIMEOUT, naive)


# ------------------------------------------------------------------------------------------------ replies

def test_the_first_reply_is_recorded_with_its_time():
    q = STATES["asked"]().with_reply("  it is Y now \n", NOW)
    assert (q.reply_text, q.reply_at) == ("it is Y now", NOW)
    assert q.reply_not_shown


def test_a_second_reply_is_joined_by_a_new_line_and_moves_the_time():
    later = NOW + timedelta(minutes=5)
    q = STATES["asked_replied"]().with_reply("and it was 2019", later)
    assert q.reply_text == "it is Y now\nand it was 2019"
    assert q.reply_at == later


def test_a_reply_that_overflows_is_cut_at_the_bound():
    first = "a" * (MAX_REPLY_CHARS - 10)
    q = STATES["asked"]().with_reply(first, NOW).with_reply("b" * 50, NOW + timedelta(minutes=1))
    assert len(q.reply_text) == MAX_REPLY_CHARS
    assert q.reply_text == first + "\n" + "b" * 9
    assert q.reply_at == NOW + timedelta(minutes=1)


def test_a_single_reply_longer_than_the_bound_is_cut_to_it():
    q = STATES["asked"]().with_reply("c" * (MAX_REPLY_CHARS + 500), NOW)
    assert q.reply_text == "c" * MAX_REPLY_CHARS


def test_a_reply_that_exactly_fills_the_bound_is_kept_whole():
    assert len(STATES["asked"]().with_reply("d" * MAX_REPLY_CHARS, NOW).reply_text) == MAX_REPLY_CHARS


def test_words_that_do_not_fit_at_all_change_nothing_not_even_the_time():
    full = STATES["asked"]().with_reply("e" * MAX_REPLY_CHARS, NOW)
    assert full.with_reply("more", NOW + timedelta(hours=1)) == full
    almost = STATES["asked"]().with_reply("e" * (MAX_REPLY_CHARS - 1), NOW)
    assert almost.with_reply("more", NOW + timedelta(hours=1)) == almost


@pytest.mark.parametrize("words", ["", "   ", "\n\t"])
def test_a_reply_with_no_words_is_not_recorded(words):
    with pytest.raises(QuestionTransitionError, match="no words"):
        STATES["asked"]().with_reply(words, NOW)


def test_offered_marks_the_reply_shown_and_closes_the_door_on_more_words():
    shown = STATES["asked_replied"]().offered(NOW)
    assert shown.offered_at == NOW and not shown.reply_not_shown
    with pytest.raises(QuestionTransitionError):
        shown.with_reply("too late", NOW)


# ------------------------------------------------------------------------------------------------ closing

@pytest.mark.parametrize("state", ["queued", "pending_new", "pending_posted", "asked", "unanswered"])
def test_closing_sets_the_purge_time_from_the_retention(state):
    closed = STATES[state]().closed(ClosedBy.RESOLVED_ELSEWHERE, NOW)
    assert (closed.status, closed.closed_at, closed.closed_by) == (Q.CLOSED, NOW, ClosedBy.RESOLVED_ELSEWHERE)
    # The purge time is the store's TTL field since S4 T4b, written at serialisation from `closed_at`.
    assert question_document(closed)["purge_after"] == NOW + timedelta(days=90) == NOW + QUESTION_RETENTION
    assert not closed.is_open


@pytest.mark.parametrize("source,state", [("digit", "asked"), ("button", "asked"), ("button", "pending_posted"),
                                           ("cabinet", "asked"), ("cabinet", "queued"), ("cabinet", "pending_new")])
def test_an_owner_close_carries_his_answer_and_where_it_came_from(source, state):
    closed = STATES[state]().closed(ClosedBy.OWNER, NOW, AnswerKind.NEVER, source)
    assert (closed.answer, closed.answer_source, closed.closed_by) == (AnswerKind.NEVER, source, ClosedBy.OWNER)


@pytest.mark.parametrize("args", [
    (ClosedBy.OWNER, NOW, None, "digit"),
    (ClosedBy.OWNER, NOW, AnswerKind.NEVER, None),
    (ClosedBy.OWNER, NOW, AnswerKind.NEVER, "review"),
    (ClosedBy.REVIEW, NOW, AnswerKind.NEVER, None),
    (ClosedBy.TIMEOUT, NOW, AnswerKind.NEVER, None),
    (ClosedBy.TIMEOUT, NOW, None, "digit"),
    (ClosedBy.RESOLVED_ELSEWHERE, NOW, AnswerKind.NEVER, "cabinet"),
    (ClosedBy.RESOLVED_ELSEWHERE, NOW, None, "cabinet"),
    (ClosedBy.REVIEW, NOW, None, "digit"),
    (ClosedBy.REVIEW, NOW, None, "cabinet"),
])
def test_closings_that_do_not_make_sense_are_refused_by_the_move(args):
    with pytest.raises(QuestionTransitionError):
        STATES["asked_replied"]().closed(*args)


def test_a_review_close_on_the_owners_words_says_so_and_a_plain_review_close_has_no_source():
    on_words = STATES["asked_replied"]().closed(ClosedBy.REVIEW, NOW, source="review")
    assert (on_words.answer, on_words.answer_source) == (None, "review")
    plain = STATES["asked"]().closed(ClosedBy.REVIEW, NOW)
    assert (plain.answer, plain.answer_source) == (None, None)
    with pytest.raises(QuestionTransitionError):
        STATES["asked"]().closed(ClosedBy.REVIEW, NOW, source="review")


def test_only_a_question_that_was_asked_times_out():
    with pytest.raises(QuestionTransitionError, match="times out"):
        STATES["queued"]().closed(ClosedBy.TIMEOUT, NOW)
    with pytest.raises(QuestionTransitionError, match="times out"):
        STATES["pending_posted"]().closed(ClosedBy.TIMEOUT, NOW)
    assert STATES["unanswered"]().closed(ClosedBy.TIMEOUT, NOW).closed_by is ClosedBy.TIMEOUT


def test_a_digit_or_a_button_needs_the_message_it_answered():
    with pytest.raises(QuestionTransitionError, match="message_ref"):
        STATES["queued"]().closed(ClosedBy.OWNER, NOW, AnswerKind.NEVER, "digit")
    with pytest.raises(QuestionTransitionError, match="message_ref"):
        STATES["pending_new"]().closed(ClosedBy.OWNER, NOW, AnswerKind.NEVER, "button")


# ------------------------------------------------------------------------------------------------ reads

@pytest.mark.parametrize("state,is_open,delivered,answerable,retraction", [
    ("queued", True, False, False, False),
    ("pending_new", True, False, False, False),
    ("pending_appended", True, False, False, True),
    ("pending_posted", True, True, True, False),
    ("asked", True, True, True, False),
    ("unanswered", True, True, True, False),
    ("closed", False, True, False, False),
])
def test_what_a_question_is_by_its_state(state, is_open, delivered, answerable, retraction):
    q = STATES[state]()
    assert (q.is_open, q.delivered, q.answerable, q.retraction_due) == (is_open, delivered, answerable, retraction)


def test_a_closed_question_that_was_never_posted_is_not_delivered():
    q = STATES["pending_appended"]().closed(ClosedBy.RESOLVED_ELSEWHERE, NOW)
    assert not q.delivered and not q.answerable and not q.retraction_due


def test_the_statuses_and_closers_are_the_ones_the_plan_names():
    assert [s.value for s in QuestionStatus] == ["queued", "pending", "asked", "unanswered", "closed"]
    assert [c.value for c in ClosedBy] == ["review", "timeout", "owner", "resolved_elsewhere"]
    assert [a.value for a in AnswerKind] == [
        "still_true", "ended", "never", "dont_know",
        # P2 revised (C2): the answers of the happened, renewed and paid questions, appended in this order
        "happened", "not_happened", "renewed", "not_renewed", "not_needed", "paid", "not_paid",
    ]


def test_the_timeout_is_fourteen_days_and_the_retention_ninety():
    assert QUESTION_TIMEOUT == timedelta(days=14)
    assert QUESTION_RETENTION == timedelta(days=90)


@pytest.mark.parametrize("state", ["asked", "unanswered"])
def test_a_sent_question_times_out_fourteen_days_after_it_was_asked(state):
    asked = NOW - QUESTION_TIMEOUT + timedelta(seconds=1)
    q = STATES[state]().model_copy(update={"asked_at": asked})
    assert not q.is_timed_out(NOW)
    assert q.is_timed_out(NOW + timedelta(seconds=1))
    assert q.model_copy(update={"asked_at": NOW - QUESTION_TIMEOUT}).is_timed_out(NOW)


@pytest.mark.parametrize("state", ["asked_replied", "asked_offered"])
def test_a_reply_never_shortens_the_wait(state):
    q = STATES[state]().model_copy(update={"asked_at": NOW - timedelta(days=13)})
    assert not q.is_timed_out(NOW)


@pytest.mark.parametrize("state", ["queued", "pending_new", "pending_posted", "closed"])
def test_a_question_that_is_not_sent_or_not_open_never_times_out(state):
    assert not STATES[state]().is_timed_out(NOW + timedelta(days=5000))


def test_a_recorded_reply_is_not_shown_until_the_review_has_offered_it():
    assert STATES["asked_replied"]().reply_not_shown
    assert not STATES["asked_offered"]().reply_not_shown
    assert not STATES["asked"]().reply_not_shown


def test_a_closed_question_has_no_reply_waiting_for_the_review():
    assert not STATES["asked_replied"]().closed(ClosedBy.RESOLVED_ELSEWHERE, NOW).reply_not_shown


# ------------------------------------------------------------------------------------------------ invariants: the status

@pytest.mark.parametrize("name,value", [
    ("session_id", "s"), ("appended_at", T_APPEND), ("message_ref", REF), ("asked_at", T_ASKED),
    ("reply_text", "words"), ("reply_at", T_ASKED), ("offered_at", T_ASKED),
])
def test_a_queued_question_has_no_attempt(name, value):
    with pytest.raises(ValidationError, match="queued question has no attempt"):
        make(**{name: value})


def test_a_pending_question_has_a_session_and_has_not_been_asked():
    with pytest.raises(ValidationError, match="pending question has a session_id"):
        make(status=Q.PENDING)
    with pytest.raises(ValidationError, match="not been asked yet"):
        make(status=Q.PENDING, session_id="s", asked_at=T_ASKED)


@pytest.mark.parametrize("status,missing", list(product([Q.ASKED, Q.UNANSWERED], ["asked_at", "session_id", "appended_at", "message_ref"])))
def test_a_sent_question_has_everything_the_send_recorded(status, missing):
    fields = {**SENT, missing: None}
    with pytest.raises(ValidationError, match="asked_at, session_id, appended_at and a message_ref"):
        make(status=status, **fields)


# ------------------------------------------------------------------------------------------------ invariants: replies

def test_a_pending_question_with_a_message_was_appended_first():
    with pytest.raises(ValidationError, match="posted question was appended first"):
        make(status=Q.PENDING, session_id="u1:D1", message_ref=REF)
    make(status=Q.PENDING, session_id="u1:D1", appended_at=T_APPEND, message_ref=REF)
    make(status=Q.PENDING, session_id="u1:D1", appended_at=T_APPEND)


def test_a_reply_and_its_time_go_together():
    with pytest.raises(ValidationError, match="go together"):
        make(status=Q.ASKED, reply_text="words", **SENT)
    with pytest.raises(ValidationError, match="go together"):
        make(status=Q.ASKED, reply_at=NOW, **SENT)


def test_a_reply_needs_a_question_that_was_asked():
    with pytest.raises(ValidationError, match="needs a question that was asked"):
        make(status=Q.PENDING, session_id="s", appended_at=T_APPEND, message_ref=REF, reply_text="words", reply_at=NOW)


def test_offered_needs_a_reply():
    with pytest.raises(ValidationError, match="offered_at needs a reply"):
        make(status=Q.ASKED, offered_at=NOW, **SENT)


def test_a_reply_cannot_be_empty_or_longer_than_the_bound():
    with pytest.raises(ValidationError):
        make(status=Q.ASKED, reply_text="", reply_at=NOW, **SENT)
    make(status=Q.ASKED, reply_text="x" * MAX_REPLY_CHARS, reply_at=NOW, **SENT)
    with pytest.raises(ValidationError):
        make(status=Q.ASKED, reply_text="x" * (MAX_REPLY_CHARS + 1), reply_at=NOW, **SENT)


# ------------------------------------------------------------------------------------------------ invariants: closing

def closed_fields(by, **over):
    fields = dict(status=Q.CLOSED, closed_at=NOW, closed_by=by)
    fields.update(over)
    return fields


@pytest.mark.parametrize("name,value", [
    ("closed_at", NOW), ("closed_by", ClosedBy.REVIEW), ("answer", AnswerKind.ENDED),
    ("answer_source", "cabinet"),
])
def test_only_a_closed_question_carries_a_closing(name, value):
    with pytest.raises(ValidationError, match="only a closed question"):
        make(status=Q.ASKED, **SENT, **{name: value})


def test_a_closed_question_says_when_and_by_whom():
    with pytest.raises(ValidationError, match="closed_at and closed_by"):
        make(**closed_fields(None))
    with pytest.raises(ValidationError, match="closed_at and closed_by"):
        make(**closed_fields(ClosedBy.RESOLVED_ELSEWHERE, closed_at=None))


@pytest.mark.parametrize("retention", [timedelta(seconds=1), timedelta(days=30), timedelta(days=365)])
def test_a_question_stored_under_another_retention_still_reads(retention):
    # The stored purge time is the store's TTL field since S4 T4b; its decoder ignores it.
    closed = make(**closed_fields(ClosedBy.RESOLVED_ELSEWHERE))
    stored = {**question_document(closed), "purge_after": NOW + retention}
    assert question_from(stored) == closed
    assert FactReviewQuestion.model_validate(closed.model_dump()) == closed


def test_an_owner_close_needs_an_answer_and_a_source_of_his_ways():
    with pytest.raises(ValidationError, match="owner close has an answer"):
        make(**closed_fields(ClosedBy.OWNER, answer_source="cabinet"))
    with pytest.raises(ValidationError, match="owner close has an answer"):
        make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER))
    with pytest.raises(ValidationError, match="owner close has an answer"):
        make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER, answer_source="review", reply_text="w", reply_at=NOW,
                             **{k: v for k, v in SENT.items()}))
    make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER, answer_source="cabinet"))


def test_a_digit_or_button_answer_needs_the_message_but_a_cabinet_answer_does_not():
    for source in ("digit", "button"):
        with pytest.raises(ValidationError, match="needs the question's message_ref"):
            make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER, answer_source=source))
        make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER, answer_source=source, message_ref=REF))
    make(**closed_fields(ClosedBy.OWNER, answer=AnswerKind.NEVER, answer_source="cabinet"))


@pytest.mark.parametrize("by", [ClosedBy.REVIEW, ClosedBy.TIMEOUT, ClosedBy.RESOLVED_ELSEWHERE])
def test_only_an_owner_close_has_an_answer(by):
    with pytest.raises(ValidationError, match="only an owner close has an answer"):
        make(**closed_fields(by, answer=AnswerKind.NEVER, **SENT))


def test_a_review_close_has_the_source_review_with_the_owners_words_or_none():
    words = dict(reply_text="words", reply_at=NOW, **SENT)
    with pytest.raises(ValidationError, match="needs a reply_text"):
        make(**closed_fields(ClosedBy.REVIEW, answer_source="review", **SENT))
    make(**closed_fields(ClosedBy.REVIEW, answer_source="review", **words))
    make(**closed_fields(ClosedBy.REVIEW, **SENT))
    for source in ("digit", "button", "cabinet"):
        with pytest.raises(ValidationError, match="source review or none"):
            make(**closed_fields(ClosedBy.REVIEW, answer_source=source, **words))


@pytest.mark.parametrize("by", [ClosedBy.TIMEOUT, ClosedBy.RESOLVED_ELSEWHERE])
@pytest.mark.parametrize("source", ["digit", "button", "cabinet", "review"])
def test_a_timeout_or_an_elsewhere_close_has_no_answer_source(by, source):
    with pytest.raises(ValidationError, match="no answer source"):
        make(**closed_fields(by, answer_source=source, **SENT))


def test_a_timeout_needs_a_question_that_was_asked_an_elsewhere_close_does_not():
    with pytest.raises(ValidationError, match="only a question that was asked times out"):
        make(**closed_fields(ClosedBy.TIMEOUT))
    make(**closed_fields(ClosedBy.TIMEOUT, **SENT))
    make(**closed_fields(ClosedBy.RESOLVED_ELSEWHERE))


# ------------------------------------------------------------------------------------------------ invariants: the rest

def test_a_fallback_date_or_noise_belongs_to_an_archive_fallback():
    with pytest.raises(ValidationError, match="archive fallback"):
        make(fallback="revive", fallback_date=date(2026, 9, 1))
    with pytest.raises(ValidationError, match="archive fallback"):
        make(fallback="revive", noise=True)
    q = make(fallback="archive", fallback_date=date(2026, 9, 1), noise=True)
    assert q.fallback_date == date(2026, 9, 1) and q.noise
    assert make(fallback="archive").fallback_date is None


def test_the_fallback_is_revive_or_archive():
    with pytest.raises(ValidationError):
        make(fallback="invalidate")


def test_the_quoted_text_and_the_ids_cannot_be_empty():
    for name in ("question_id", "account_id", "user_id", "fact_id", "quoted_text"):
        with pytest.raises(ValidationError):
            make(**{name: ""})
    with pytest.raises(ValidationError):
        make(status=Q.PENDING, session_id="")


def test_the_queue_key_is_required():
    with pytest.raises(ValidationError):
        FactReviewQuestion(question_id="q", account_id="a", user_id="u", fact_id="f", quoted_text="t",
                           fallback="revive", queued_at=NOW)


NAIVE = datetime(2026, 10, 24, 9, 0)


@pytest.mark.parametrize("field,build", [
    ("queue_key", lambda: make(queue_key=NAIVE)),
    ("queued_at", lambda: make(queued_at=NAIVE)),
    ("appended_at", lambda: make(status=Q.PENDING, session_id="s", appended_at=NAIVE)),
    ("asked_at", lambda: make(status=Q.ASKED, **{**SENT, "asked_at": NAIVE})),
    ("reply_at", lambda: make(status=Q.ASKED, reply_text="w", reply_at=NAIVE, **SENT)),
    ("offered_at", lambda: make(status=Q.ASKED, reply_text="w", reply_at=NOW, offered_at=NAIVE, **SENT)),
    ("closed_at", lambda: make(**closed_fields(ClosedBy.RESOLVED_ELSEWHERE, closed_at=NAIVE))),
])
def test_a_naive_time_is_refused_when_the_question_is_built(field, build):
    with pytest.raises(ValidationError, match="timezone"):
        build()


# ------------------------------------------------------------------------------------------------ frozen, and no way round

def test_the_question_is_a_value_that_does_not_change_in_place():
    with pytest.raises(ValidationError):
        make().status = Q.CLOSED


def test_model_copy_validates_again_so_it_cannot_make_an_illegal_question():
    asked = STATES["asked"]()
    with pytest.raises(ValidationError, match="closed_at and closed_by"):
        asked.model_copy(update={"status": Q.CLOSED})
    with pytest.raises(ValidationError):
        asked.model_copy(update={"reply_text": "x" * (MAX_REPLY_CHARS + 1), "reply_at": NOW})
    with pytest.raises(ValidationError, match="queued question has no attempt"):
        asked.model_copy(update={"status": Q.QUEUED})
    with pytest.raises(ValidationError, match="timezone"):
        asked.model_copy(update={"asked_at": NAIVE})


def test_model_copy_still_makes_a_legal_copy_and_leaves_the_original():
    asked = STATES["asked"]()
    other = asked.model_copy(update={"quoted_text": "Lives in Porto"})
    assert other.quoted_text == "Lives in Porto" and asked.quoted_text == "Lives in Lisbon"
    assert asked.model_copy() == asked
    assert asked.model_copy(deep=True) == asked


def test_the_question_round_trips_through_its_dump_and_json_with_the_message_ref():
    q = STATES["asked_replied"]().closed(ClosedBy.REVIEW, NOW, source="review")
    assert FactReviewQuestion.model_validate(q.model_dump()) == q
    assert FactReviewQuestion.model_validate_json(q.model_dump_json()) == q
    assert q.message_ref == REF
