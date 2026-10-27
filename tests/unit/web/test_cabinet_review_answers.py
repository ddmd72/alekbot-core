"""Facts S4 · T11b: `POST /api/user/facts/review-answers` (plan §3.18), through the real route and the real
`FactAnswerService` over the review world (the evaluating Firestore fake behind the real repository, typed write path, review
store, counters and task queue). Assertions are on the stored documents, the stored questions and state, the created
tasks and the response. One answer of each kind is applied under the owner with source `cabinet`; a malformed body or a
question of another user refuses the whole request with nothing written; an answer to the current question releases the next
one, and one to a queued question takes it out of the queue."""
import copy
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock

import pytest
from quart import Quart

from src.domain.entities import ExpiryKind, FactState
from src.domain.fact_review_answer_kind import AnswerKind
from src.domain.fact_review_language import fact_question_message
from src.domain.fact_review_question import FactReviewQuestion, QuestionStatus
from src.domain.language import LanguageCode
from src.domain.fact_review_reasons import answer_reason
from src.domain.owner_timezone import ResolvedOwnerZone
from src.domain.ui_messages import UIMessage
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.localization_service import LocalizationService
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_review_world import ReviewWorld
from tests.support.fact_write import EXPIRY_NONE, ctx

TOKEN = {"Authorization": "Bearer t"}
TEXT = "drinks green tea every morning"


class FixedZones:
    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(USER, "Europe/Lisbon")


def _service(world: ReviewWorld) -> FactAnswerService:
    texts = FactQuestionTexts(world.language, world.localization, world.review_store, world.counters, LanguageCode.EN,
                              clock=world.clock)
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=world.review_store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store, texts=texts, interactive=world.interactive,
        owner_zones=FixedZones(),
    )


def _app(world: ReviewWorld, account: str = ACC, user: str = USER, service: Any = "real", with_store: bool = True,
         localized: bool = True) -> Quart:
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": user, "account_id": account, "role": "owner"})
    app = Quart("test_cabinet_review_answers")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=world.repo,
        embedding_service=MagicMock(),
        fact_review_store=world.review_store if with_store else None,
        fact_answer_service=_service(world) if service == "real" else service,
        language_service=world.language if localized else None,
        localization=LocalizationService(world.localization) if localized else None,
    ))
    return app


async def _post(world: ReviewWorld, body: Any, **kwargs: Any):
    async with _app(world, **kwargs).test_client() as client:
        resp = await client.post("/api/user/facts/review-answers", json=body, headers=TOKEN)
        return resp.status_code, await resp.get_json()


def _answers(*pairs: Any) -> Dict[str, Any]:
    return {"answers": [{"question_id": q, "answer": a} for q, a in pairs]}


async def _standing(world: ReviewWorld, text: str = TEXT) -> str:
    """A standing fact (a horizon) the janitor staled."""
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14, reported_date=NOW - timedelta(days=60))


async def _ask(world: ReviewWorld, fact_id: str, question_id: str = "q1", status: QuestionStatus = QuestionStatus.ASKED,
               **over: Any) -> FactReviewQuestion:
    fields: Dict[str, Any] = {"question_id": question_id, "account_id": ACC, "user_id": USER, "fact_id": fact_id,
                              "quoted_text": world.docs[fact_id]["text"], **over}
    if status is not QuestionStatus.QUEUED:
        draft = FactReviewQuestion(queue_key=NOW, queued_at=NOW, fallback="revive", **fields)
        labels = await FactQuestionTexts(world.language, world.localization, world.review_store, world.counters,
                                         LanguageCode.EN).labels_for(USER, ACC)
        fields["message_ref"] = await world.interactive.post("slack", "D1", fact_question_message(draft, labels))
    return await world.seed_question(status, **fields)


def _stored(world: ReviewWorld, question_id: str = "q1") -> dict:
    return world.question_docs[question_id]


def _tasks(world: ReviewWorld) -> List[dict]:
    return [body for task_type, body in world.enqueued if task_type == "fact_review"]


def _snapshot(world: ReviewWorld):
    return copy.deepcopy((world.docs, world.question_docs, world.state_doc(ACC), world.enqueued))


# ------------------------------------------------------------------------------------------------ each answer

@pytest.mark.parametrize("answer, state", [("still_true", "current"), ("ended", "archived"), ("never", "invalidated")])
async def test_each_answer_is_applied_under_the_owner_with_the_source_cabinet(answer, state):
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)

    status, body = await _post(world, _answers(("q1", answer)))

    assert (status, body) == (200, {"results": [{"question_id": "q1", "result": "applied"}]})
    assert world.docs[fact]["state"] == state
    event = world.docs[fact]["lifecycle_events"][-1]
    assert (event["actor"], event["reason"]) == ("owner", answer_reason(AnswerKind(answer), "q1", "cabinet"))
    stored = _stored(world)
    assert (stored["status"], stored["closed_by"], stored["answer"], stored["answer_source"]) == (
        "closed", "owner", answer, "cabinet")


async def test_no_longer_on_a_standing_fact_adds_the_history_fact():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)
    before = set(world.docs)

    await _post(world, _answers(("q1", "ended")))

    [history] = set(world.docs) - before
    assert world.docs[history]["text"].startswith("No longer true (the owner said so in ")
    assert world.docs[history]["state"] == "current"


async def test_no_longer_on_a_dated_plan_archives_it_and_adds_nothing():
    world = ReviewWorld()
    plan = await world.seed_stale("has a dentist visit on the 20th of October")
    assert world.docs[plan]["expiry_kind"] == "date"
    await _ask(world, plan)
    before = set(world.docs)

    status, body = await _post(world, _answers(("q1", "ended")))

    assert body["results"][0]["result"] == "applied"
    assert set(world.docs) == before and world.docs[plan]["state"] == "archived"


async def test_the_answer_is_counted_as_given_in_the_cabinet():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)

    await _post(world, _answers(("q1", "still_true")))

    [row] = [r for r in await world.counters.for_day(datetime.now(timezone.utc).date()) if r.account_id == ACC]
    assert row.answered_in_cabinet == 1 and row.answered_by_digit == 0


# ------------------------------------------------------------------------------------------------ bulk

async def test_a_bulk_of_three_answers_each_question_with_its_own_answer_in_request_order():
    world = ReviewWorld()
    facts = [await _standing(world, f"{TEXT} {n}") for n in range(3)]
    for n, fact in enumerate(facts):
        await _ask(world, fact, f"q{n}", QuestionStatus.QUEUED if n else QuestionStatus.ASKED)

    status, body = await _post(world, _answers(("q2", "never"), ("q0", "still_true"), ("q1", "ended")))

    assert status == 200
    assert body["results"] == [{"question_id": q, "result": "applied"} for q in ("q2", "q0", "q1")]
    assert [world.docs[f]["state"] for f in facts] == ["current", "archived", "invalidated"]
    assert all(_stored(world, f"q{n}")["status"] == "closed" for n in range(3))


async def test_fifty_answers_are_accepted():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)
    answers = _answers(("q1", "still_true"), *[(f"missing-{n}", "never") for n in range(49)])

    status, body = await _post(world, answers)

    assert status == 200 and len(body["results"]) == 50
    assert [r["result"] for r in body["results"]].count("not_found") == 49


# ------------------------------------------------------------------------------------------------ malformed

@pytest.mark.parametrize("body", [
    None, [], "x", {}, {"answers": None}, {"answers": {}}, {"answers": []},
    {"answers": ["q1"]}, {"answers": [{"answer": "never"}]}, {"answers": [{"question_id": "q1"}]},
    {"answers": [{"question_id": "", "answer": "never"}]}, {"answers": [{"question_id": 7, "answer": "never"}]},
    {"answers": [{"question_id": "q1", "answer": "maybe"}]}, {"answers": [{"question_id": "q1", "answer": "dont_know"}]},
    {"answers": [{"question_id": "q1", "answer": "NEVER"}]}, {"answers": [{"question_id": "q1", "answer": None}]},
    {"answers": [{"question_id": "q1", "answer": "never"}, {"question_id": "q1", "answer": "ended"}]},
    {"answers": [{"question_id": "q1", "answer": "never"}, {"question_id": "q2"}]},
    {"answers": [{"question_id": f"q{n}", "answer": "never"} for n in range(51)]},
])
async def test_a_malformed_body_is_a_400_and_nothing_is_written(body):
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)
    before = _snapshot(world)

    status, payload = await _post(world, body)

    assert status == 400 and "error" in payload
    assert _snapshot(world) == before


async def test_a_body_that_is_not_json_is_a_400():
    world = ReviewWorld()
    async with _app(world).test_client() as client:
        resp = await client.post("/api/user/facts/review-answers", data="not json",
                                 headers={**TOKEN, "Content-Type": "application/json"})

    assert resp.status_code == 400


# ------------------------------------------------------------------------------------------------ whose question

async def test_another_users_question_in_the_account_is_a_403_and_nothing_is_written_even_beside_a_valid_one():
    world = ReviewWorld()
    mine = await _standing(world, TEXT)
    theirs = await _standing(world, "walks to the pool at dawn")
    await _ask(world, mine, "q-mine")
    await _ask(world, theirs, "q-colleague", user_id="usr-colleague")
    before = _snapshot(world)

    status, body = await _post(world, _answers(("q-mine", "never"), ("q-colleague", "never")))

    assert (status, body) == (403, {"error": "Not authorized"})
    assert _snapshot(world) == before


async def test_another_accounts_question_is_not_found_and_nothing_is_written():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact, "q-theirs", account_id=OTHER, user_id="usr-other")
    before = _snapshot(world)

    status, body = await _post(world, _answers(("q-theirs", "never")))

    assert (status, body["results"]) == (200, [{"question_id": "q-theirs", "result": "not_found"}])
    assert _snapshot(world) == before


async def test_an_unknown_question_is_not_found():
    world = ReviewWorld()

    _, body = await _post(world, _answers(("no-such", "still_true")))

    assert body["results"] == [{"question_id": "no-such", "result": "not_found"}]


# ------------------------------------------------------------------------------------------------ per-item results

async def test_a_closed_question_is_not_open_and_changes_nothing():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact, status=QuestionStatus.CLOSED)
    before = _snapshot(world)

    _, body = await _post(world, _answers(("q1", "never")))

    assert body["results"] == [{"question_id": "q1", "result": "not_open"}]
    assert _snapshot(world) == before


async def test_a_fact_whose_text_changed_since_the_question_is_changed_and_nothing_is_written():
    world = ReviewWorld()
    fact = await world.seed(TEXT)
    await _ask(world, fact, status=QuestionStatus.QUEUED)
    await world.store.adapter.update_fact(fact, {"content": "drinks black tea every morning", "expiry": EXPIRY_NONE}, ACC,
                                          context=ctx())
    before = _snapshot(world)

    _, body = await _post(world, _answers(("q1", "never")))

    assert body["results"] == [{"question_id": "q1", "result": "changed"}]
    assert _snapshot(world) == before and _stored(world)["status"] == "queued"


async def test_one_item_failing_is_reported_and_the_others_still_apply():
    world = ReviewWorld()
    first = await _standing(world, TEXT)
    second = await _standing(world, "walks to the pool at dawn")
    await _ask(world, first, "q1")
    await _ask(world, second, "q2", QuestionStatus.QUEUED)
    real = _service(world)

    class FailsOnFirst:
        async def apply(self, account_id, user_id, question_id, *rest):
            if question_id == "q1":
                raise RuntimeError("store unavailable: secret detail")
            return await real.apply(account_id, user_id, question_id, *rest)

    status, body = await _post(world, _answers(("q1", "never"), ("q2", "never")), service=FailsOnFirst())

    assert status == 200
    assert body["results"] == [{"question_id": "q1", "result": "error"}, {"question_id": "q2", "result": "applied"}]
    assert "secret" not in str(body) and world.docs[first]["state"] == "stale"


# ------------------------------------------------------------------------------------------------ the queue

async def test_an_answer_to_the_current_question_releases_the_next_one():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)
    await world.seed_state(account_id=ACC, current_question_id="q1", sent=1, sent_on=NOW.date())

    await _post(world, _answers(("q1", "never")))

    assert world.state_doc(ACC)["current_question_id"] is None
    assert _tasks(world) == [{"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "next"}]


async def test_an_answer_to_a_queued_question_releases_nothing_and_takes_it_out_of_the_queue():
    world = ReviewWorld()
    current = await _standing(world, TEXT)
    queued = await _standing(world, "walks to the pool at dawn")
    await _ask(world, current, "q-current")
    await _ask(world, queued, "q-queued", QuestionStatus.QUEUED)
    await world.seed_state(account_id=ACC, current_question_id="q-current", sent=1, sent_on=NOW.date())

    await _post(world, _answers(("q-queued", "still_true")))

    assert _tasks(world) == [] and world.state_doc(ACC)["current_question_id"] == "q-current"
    assert _stored(world, "q-queued")["status"] == "closed" and _stored(world, "q-current")["status"] == "asked"
    async with _app(world).test_client() as client:
        listed = await (await client.get("/api/user/facts/review-queue", headers=TOKEN)).get_json()
    assert [q["question_id"] for q in listed["questions"]] == ["q-current"]


# ------------------------------------------------------------------------------------------------ unavailable

async def test_without_the_answer_service_the_route_is_a_501_and_writes_nothing():
    world = ReviewWorld()
    fact = await _standing(world)
    await _ask(world, fact)
    before = _snapshot(world)

    status, body = await _post(world, _answers(("q1", "never")), service=None)

    assert status == 501 and "not configured" in body["error"]
    assert _snapshot(world) == before


async def test_without_the_review_store_the_route_is_a_501():
    world = ReviewWorld()

    status, _ = await _post(world, _answers(("q1", "never")), with_store=False)

    assert status == 501


async def test_a_store_that_fails_before_anything_is_applied_is_a_500_without_details():
    world = ReviewWorld()
    world.review_store.get_questions = MagicMock(side_effect=RuntimeError("store down: secret detail"))  # type: ignore[method-assign]

    status, body = await _post(world, _answers(("q1", "never")))

    assert status == 500 and body == {"error": "Internal server error"}


# ------------------------------------------------------------------------------------------------ the buttons' labels

async def _options(world: ReviewWorld, **kwargs: Any) -> Optional[list]:
    """P2 revised (C15): the options ride on each listed question, by its kind; the view has no top-level options. The
    world gets one still_true question in the queue, whose options are read."""
    await _ask(world, await _standing(world), "q1", QuestionStatus.QUEUED)
    async with _app(world, **kwargs).test_client() as client:
        body = await (await client.get("/api/user/facts/review-queue", headers=TOKEN)).get_json()
    assert "options" not in body
    [listed] = body["questions"]
    assert (listed["question_id"], listed["kind"], listed["group"]) == ("q1", "still_true", "questions")
    return listed["options"]


async def test_the_queue_view_offers_the_three_deciding_answers_in_the_users_ui_language():
    world = ReviewWorld()
    world.language.preferences[USER] = (LanguageCode.UK, False)

    options = await _options(world)

    expected = [world.localization.get_ui_string(LanguageCode.UK, m) for m in (
        UIMessage.MEMORY_OPTION_STILL_TRUE, UIMessage.MEMORY_OPTION_ENDED, UIMessage.MEMORY_OPTION_NEVER)]
    assert [(o["answer"], o["label"]) for o in options] == list(zip(["still_true", "ended", "never"], expected))
    assert all(o["label"] != "Yes" for o in options)


async def test_without_a_localization_the_labels_are_the_english_ones():
    world = ReviewWorld()

    options = await _options(world, localized=False)

    assert [(o["answer"], o["label"]) for o in options] == [
        ("still_true", "Yes"), ("ended", "No longer"), ("never", "Never")]
