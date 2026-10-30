"""Facts S4 · T11a: `GET /api/user/facts/review-queue` (plan §3.18), through the real route, repository and review store on the
evaluating Firestore fake. The user's open questions come in the order they are dealt with (the question out now, those sent
and unanswered, then the queue in its order), each with its fact in full as it stands now (the text the question quoted when the fact is gone or was rewritten to
a text the owner was not asked about), its domain, `reported_date`,
since when it waits and its status. Another account's and another user's questions are never listed; no store is a 501."""
import copy
from datetime import timedelta
from typing import Any, List
from unittest.mock import MagicMock

from quart import Quart

from src.domain.entities import ExpiryKind
from src.domain.fact_review_question import QuestionStatus
from src.domain.language import LanguageCode
from src.domain.owner_timezone import ResolvedOwnerZone
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.web.user_cabinet_app import create_user_cabinet_blueprint
from tests.support.fact_kit import ACC, OTHER, USER
from tests.support.fact_lifecycle_world import NOW
from tests.support.fact_write import EXPIRY_NONE, ctx
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import REF
from src.composition.connector_registry import ConnectorRegistry

TOKEN = {"Authorization": "Bearer t"}
TEXT_TEA = "drinks green tea every morning"
TEXT_MARKET = "buys bread at the Saturday market"


class FixedZones:
    async def resolve(self, account_id: str) -> ResolvedOwnerZone:
        return ResolvedOwnerZone(USER, "Europe/Lisbon")


def _service(world: ReviewWorld, store: Any) -> Any:
    """The answer service whose `review_queue` the route reads, over `store` (none: no service)."""
    if store is None:
        return None
    texts = FactQuestionTexts(world.language, world.localization, store, world.counters, LanguageCode.EN,
                              clock=world.clock)
    return FactAnswerService(
        fact_updates=world.store.adapter, repo=world.repo, review_store=store, counters=world.counters,
        task_queue=world.queue, session_store=world.session_store, texts=texts, interactive=world.interactive,
        owner_zones=FixedZones(),
    )


def _app(world: ReviewWorld, account: str = ACC, user: str = USER, store: Any = "world") -> Quart:
    store = world.review_store if store == "world" else store
    session_service = MagicMock()
    session_service.verify_access_token = MagicMock(return_value={"sub": user, "account_id": account, "role": "owner"})
    app = Quart("test_cabinet_review_queue_view")
    app.register_blueprint(create_user_cabinet_blueprint(
        invite_service=MagicMock(), session_service=session_service, user_repo=MagicMock(), fact_repo=world.repo,
        embedding_service=MagicMock(), fact_review_store=store, fact_answer_service=_service(world, store), connectors=ConnectorRegistry(),
    ))
    return app


async def _queue(world: ReviewWorld, **kwargs: Any):
    async with _app(world, **kwargs).test_client() as client:
        resp = await client.get("/api/user/facts/review-queue", headers=TOKEN)
        return resp.status_code, await resp.get_json()


async def _stale(world: ReviewWorld, text: str, **over: Any) -> str:
    return await world.seed_stale(text, expiry_kind=ExpiryKind.HORIZON, ttl_days=14,
                                  reported_date=NOW - timedelta(days=60), **over)


async def _ask(world: ReviewWorld, fact_id: str, question_id: str, status: QuestionStatus = QuestionStatus.QUEUED,
               **over: Any):
    fields = {"question_id": question_id, "account_id": ACC, "user_id": USER, "fact_id": fact_id,
              "quoted_text": world.docs[fact_id]["text"], **over}
    if status is not QuestionStatus.QUEUED:
        fields.setdefault("message_ref", REF)
    return await world.seed_question(status, **fields)


def _ids(body: Any) -> List[str]:
    return [q["question_id"] for q in body["questions"]]


async def test_the_current_question_comes_first_then_the_unanswered_then_the_queue_in_order():
    world = ReviewWorld()
    queued_late = await _stale(world, "is in the community-garden group with Kim")
    queued_early = await _stale(world, "waters the ferns on Sundays")
    unanswered = await _stale(world, "walks to the pool at dawn")
    current = await _stale(world, TEXT_TEA)
    await _ask(world, queued_late, "q-late", queue_key=NOW - timedelta(days=2))
    await _ask(world, queued_early, "q-early", queue_key=NOW - timedelta(days=9))
    await _ask(world, unanswered, "q-unanswered", QuestionStatus.UNANSWERED, queue_key=NOW - timedelta(days=20), asked_at=NOW - timedelta(days=3))
    await _ask(world, current, "q-current", QuestionStatus.ASKED, queue_key=NOW - timedelta(days=1), asked_at=NOW)
    await world.seed_state(account_id=ACC, current_question_id="q-current")

    status, body = await _queue(world)

    assert status == 200
    assert _ids(body) == ["q-current", "q-unanswered", "q-early", "q-late"]
    assert [q["status"] for q in body["questions"]] == ["current", "sent", "waiting", "waiting"]


async def test_each_entry_carries_the_full_current_text_domain_dates_and_waiting_since():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    key = NOW - timedelta(days=12)
    await _ask(world, fact, "q1", queue_key=key)

    _, body = await _queue(world)

    [entry] = body["questions"]
    assert entry["text"] == TEXT_TEA
    assert entry["domain"] == world.docs[fact]["domain"]
    assert entry["reported_date"] == world.docs[fact]["reported_date"].isoformat()
    assert entry["waiting_since"] == key.isoformat()
    assert entry["asked_at"] is None


async def test_a_sent_question_says_when_it_was_asked():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    asked_at = NOW - timedelta(days=3)
    await _ask(world, fact, "q1", QuestionStatus.UNANSWERED, asked_at=asked_at)

    _, body = await _queue(world)

    assert (body["questions"][0]["status"], body["questions"][0]["asked_at"]) == ("sent", asked_at.isoformat())


async def test_a_fact_rewritten_since_the_question_shows_the_text_the_question_quoted():
    world = ReviewWorld()
    fact = await world.seed(TEXT_TEA)
    await _ask(world, fact, "q1")
    await world.store.adapter.update_fact(
        fact, {"content": "drinks black tea every morning", "expiry": EXPIRY_NONE}, ACC, context=ctx(),
    )

    _, body = await _queue(world)

    assert body["questions"][0]["text"] == TEXT_TEA


async def test_a_fact_that_is_gone_shows_the_text_the_question_quoted():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    await _ask(world, fact, "q1")
    del world.docs[fact]

    status, body = await _queue(world)

    assert status == 200 and body["questions"][0]["text"] == TEXT_TEA
    assert body["questions"][0]["domain"] is None and body["questions"][0]["reported_date"] is None


async def test_a_closed_question_is_not_listed():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    await _ask(world, fact, "q-closed", QuestionStatus.CLOSED)
    other = await _stale(world, TEXT_MARKET)
    await _ask(world, other, "q-open")

    _, body = await _queue(world)

    assert _ids(body) == ["q-open"]


async def test_another_accounts_questions_are_never_listed():
    world = ReviewWorld()
    mine = await _stale(world, TEXT_TEA)
    await _ask(world, mine, "q-mine")
    theirs = await _stale(world, TEXT_MARKET)
    await _ask(world, theirs, "q-theirs", account_id=OTHER, user_id="usr-other")

    _, as_mine = await _queue(world)
    _, as_theirs = await _queue(world, account=OTHER, user="usr-other")

    assert _ids(as_mine) == ["q-mine"] and _ids(as_theirs) == ["q-theirs"]
    assert as_mine["questions"][0]["text"] == TEXT_TEA and as_theirs["questions"][0]["text"] == TEXT_MARKET


async def test_another_users_question_in_the_same_account_is_not_listed():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    await _ask(world, fact, "q-mine")
    private = await _stale(world, TEXT_MARKET)
    await _ask(world, private, "q-colleague", user_id="usr-colleague")

    _, body = await _queue(world)

    assert _ids(body) == ["q-mine"] and TEXT_MARKET not in str(body)


async def test_the_state_pointer_to_a_question_that_is_not_out_holds_nothing():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    await _ask(world, fact, "q1", QuestionStatus.UNANSWERED)
    await world.seed_state(account_id=ACC, current_question_id="q1")

    _, body = await _queue(world)

    assert body["questions"][0]["status"] == "sent"


async def test_an_account_with_no_questions_gets_an_empty_list():
    world = ReviewWorld()

    status, body = await _queue(world)

    assert (status, body["questions"]) == (200, [])


async def test_without_a_store_the_view_is_a_501():
    world = ReviewWorld()

    status, body = await _queue(world, store=None)

    assert status == 501 and "not configured" in body["error"]


async def test_the_view_writes_nothing():
    world = ReviewWorld()
    fact = await _stale(world, TEXT_TEA)
    await _ask(world, fact, "q1")
    before = (copy.deepcopy(world.docs), copy.deepcopy(world.question_docs))

    await _queue(world)

    assert (world.docs, world.question_docs) == before


async def test_a_failing_store_is_a_500_without_details():
    world = ReviewWorld()
    store = MagicMock()
    store.questions_for_account = MagicMock(side_effect=RuntimeError("store down: secret detail"))

    status, body = await _queue(world, store=store)

    assert status == 500 and body == {"error": "Internal server error"}
