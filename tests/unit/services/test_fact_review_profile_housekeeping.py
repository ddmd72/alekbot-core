"""Profile questions in the memory review's own run and store (fact profile RFC §4.7): housekeeping moves them like any
question and closes one past its 14 days `timeout`, recording nothing; every default list read stays lifecycle-only; the
claim orders lifecycle first and takes one profile question a day. On the review world, the real review store. Invented
cast."""
from datetime import date, timedelta

from src.adapters.firestore_fact_review_store import question_document, question_from
from src.domain.fact_review_frame import question_text, review_frame
from src.domain.fact_review_question import QUESTION_TIMEOUT, ClosedBy, QuestionStatus
from tests.support.fact_kit import ACC
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.profile_question_builders import profile_question
from tests.support.review_rule_builders import question as lifecycle_question
from tests.unit.services.test_fact_review_selection import USER, Rig, day_row, stored_question

TODAY = date(2026, 10, 23)


async def profile(world: ReviewWorld, kind: str = "place", status: QuestionStatus = QuestionStatus.UNANSWERED,
                  **over):
    over.setdefault("account_id", ACC)
    over.setdefault("user_id", USER)
    return await world.put_question(profile_question(kind, status, **over))


# ------------------------------------------------------------------------------------------------ the timeout

async def test_a_profile_question_past_fourteen_days_closes_timeout_and_records_nothing():
    world = ReviewWorld()
    await profile(world, "place", asked_at=NOW - QUESTION_TIMEOUT - timedelta(hours=1))
    await world.seed_state(account_id=ACC)
    docs_before = {key: dict(doc) for key, doc in world.docs.items()}

    assert await Rig(world).run() == {"status": "completed"}

    closed = await stored_question(world, "pq-place")
    assert closed.status is QuestionStatus.CLOSED and closed.closed_by is ClosedBy.TIMEOUT
    assert closed.answer is None and closed.answer_source is None and closed.reply_text is None
    assert {key: dict(doc) for key, doc in world.docs.items()} == docs_before
    row = await day_row(world)
    assert row is None or (row.timed_out, row.profile_answered, row.profile_reply_not_read) == (0, 0, 0)


async def test_a_minute_before_the_fourteen_days_the_profile_question_stays_open():
    world = ReviewWorld()
    await profile(world, "person", asked_at=NOW - QUESTION_TIMEOUT + timedelta(minutes=1))
    await world.seed_state(account_id=ACC)

    await Rig(world).run()

    assert (await stored_question(world, "pq-person")).status is QuestionStatus.UNANSWERED


async def test_every_kind_times_out_and_the_closed_question_is_kept_for_its_id():
    world = ReviewWorld()
    for kind in ("place", "person", "date"):
        await profile(world, kind, asked_at=NOW - QUESTION_TIMEOUT - timedelta(days=1))
    await world.seed_state(account_id=ACC)

    await Rig(world).run()

    for kind in ("place", "person", "date"):
        assert (await stored_question(world, f"pq-{kind}")).closed_by is ClosedBy.TIMEOUT
    assert await world.review_store.questions_for_account(ACC, scope="all") == []
    assert all("purge_after" in world.question_docs[f"pq-{kind}"] for kind in ("place", "person", "date"))


async def test_housekeeping_moves_a_profile_question_like_any_other_asked_on_an_earlier_day_is_unanswered():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.ASKED, asked_at=NOW - timedelta(days=3))
    await world.seed_state(account_id=ACC)

    await Rig(world).run()

    assert (await stored_question(world, "pq-place")).status is QuestionStatus.UNANSWERED


# ------------------------------------------------------------------------------------------------ the scoped reads

async def test_the_default_list_reads_never_return_a_profile_question():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.QUEUED)
    await world.put_question(lifecycle_question(account_id=ACC, user_id=USER, question_id="q-life"))

    assert [q.question_id for q in await world.review_store.questions_for_account(ACC)] == ["q-life"]
    assert [q.question_id for q in await world.review_store.all_open()] == ["q-life"]
    assert [q.question_id for q in await world.review_store.questions_for_account(ACC, scope="profile")] == ["pq-place"]
    assert [q.question_id for q in await world.review_store.all_open(scope="profile")] == ["pq-place"]
    assert sorted(q.question_id for q in await world.review_store.questions_for_account(ACC, scope="all")) == [
        "pq-place", "q-life"]
    assert sorted(q.question_id for q in await world.review_store.all_open(scope="all")) == ["pq-place", "q-life"]


async def test_by_message_and_get_questions_serve_both_kinds():
    world = ReviewWorld()
    asked = await profile(world, "place", QuestionStatus.ASKED)

    assert (await world.review_store.by_message(USER, asked.message_ref)).question_id == "pq-place"
    assert [q.question_id for q in await world.review_store.get_questions(["pq-place"])] == ["pq-place"]


def test_the_stored_form_carries_the_open_point_and_a_document_without_one_reads_as_a_lifecycle_question():
    question = profile_question("person")
    document = question_document(question)
    assert document["open_point"]["key"] == "person:lin-1:doe"
    assert [o["id"] for o in document["open_point"]["options"]] == ["pick1", "pick2"]
    assert question_from(document) == question
    old = question_document(lifecycle_question())
    old.pop("open_point")
    assert question_from(old).open_point is None


# ------------------------------------------------------------------------------------------------ the claim

async def claim(world: ReviewWorld, today: date = TODAY):
    return await world.review_store.claim_next(ACC, today, "u1:D1")


async def test_a_lifecycle_question_is_claimed_before_a_profile_one_whatever_the_queue_key():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.QUEUED, queue_key=NOW - timedelta(days=900))
    await world.put_question(lifecycle_question(account_id=ACC, user_id=USER, question_id="q-life", queue_key=NOW))

    claimed = await claim(world)

    assert claimed.question_id == "q-life"
    assert (await world.review_store.get_state(ACC)).profile_claimed_on is None


async def test_a_profile_claim_marks_the_day_and_counts_for_the_cap():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.QUEUED)

    claimed = await claim(world)

    assert claimed.question_id == "pq-place" and claimed.status is QuestionStatus.PENDING
    state = await world.review_store.get_state(ACC)
    assert (state.profile_claimed_on, state.sent, state.sent_on, state.current_question_id) == (
        TODAY, 1, TODAY, "pq-place")


async def test_a_profile_question_is_not_claimed_again_the_same_day_but_a_lifecycle_one_is():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.QUEUED)
    first = await claim(world)
    await world.review_store.close_and_release(first.question_id, ClosedBy.RESOLVED_ELSEWHERE, None, None, NOW)
    await profile(world, "person", QuestionStatus.QUEUED)

    assert await claim(world) is None                      # same local day: no second profile question
    await world.put_question(lifecycle_question(account_id=ACC, user_id=USER, question_id="q-life", queue_key=NOW))
    assert (await claim(world)).question_id == "q-life"


async def test_the_next_day_the_second_profile_question_goes():
    world = ReviewWorld()
    await profile(world, "place", QuestionStatus.QUEUED)
    first = await claim(world)
    await world.review_store.close_and_release(first.question_id, ClosedBy.RESOLVED_ELSEWHERE, None, None, NOW)
    await profile(world, "person", QuestionStatus.QUEUED)

    claimed = await claim(world, TODAY + timedelta(days=1))

    assert claimed.question_id == "pq-person"
    assert (await world.review_store.get_state(ACC)).profile_claimed_on == TODAY + timedelta(days=1)


# ------------------------------------------------------------------------------------------------ the send

async def test_the_daily_run_sends_a_queued_profile_question_under_its_own_frame():
    world = ReviewWorld()
    await world.notification_state.save_primary(USER, "slack", "D1")
    await profile(world, "place", QuestionStatus.QUEUED)
    await world.seed_state(account_id=ACC)

    assert await Rig(world).run() == {"status": "completed"}

    [(ref, message)] = world.interactive.posted
    assert message.prompt == "Place to confirm: Rome" and message.binding.id == "pq-place"
    sent = await stored_question(world, "pq-place")
    assert sent.status is QuestionStatus.ASKED and sent.message_ref == ref and sent.reply_text is None
    assert [(m.role, m.parts[0].text) for m in await world.session_store.messages("u1:D1")] == [
        ("user", review_frame("pq-place", profile=True)), ("model", question_text(message))]
    state = await world.review_store.get_state(ACC)
    assert (state.current_question_id, state.profile_claimed_on) == ("pq-place", date(2026, 10, 23))
    assert (await day_row(world)).questions_sent == 1
