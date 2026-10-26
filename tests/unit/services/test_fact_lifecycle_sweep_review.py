"""The janitor with the memory review wired (Facts S4 · T9a, plan §3.12, §5 T9a): the accounts it visits, the facts it leaves
to open questions, the pool under the cap and the breaker, the daily review task, the purge check.

The real service on the review world: the real repository, counters, review store and task queue over one evaluating
Firestore fake, the real owner-zone resolver over a one-account store. NOW is 2026-10-23 07:30 UTC, 08:30 in the owner's
Lisbon (summer time ends on 25 October). Assertions are on the stored facts, snapshots, state, tasks and the ops line."""
import hashlib
import logging
from dataclasses import fields
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import pytest

from src.domain.billing import BillingAccount
from src.domain.fact_lifecycle_day import CounterField, SweepSnapshot
from src.domain.fact_review_question import ClosedBy, QuestionStatus
from src.domain.fact_review_task_keys import FACT_REVIEW_TASK, daily_key
from src.domain.user import UserBotConfig, UserProfile
from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_lifecycle_world import NOW, PAST
from tests.support.fact_review_world import ReviewWorld
from tests.support.review_rule_builders import question as build_question
from tests.unit.services.test_fact_review_selection import USER, Rig, stored_question

TZ = "Europe/Lisbon"
OPEN = [QuestionStatus.QUEUED, QuestionStatus.PENDING, QuestionStatus.ASKED, QuestionStatus.UNANSWERED]


class Accounts:
    """One account, owned by USER unless `owned` is off; its defaults may carry a review interval."""

    def __init__(self, owned: bool = True, interval: Optional[int] = None) -> None:
        defaults = UserBotConfig(fact_review_interval_days=interval) if interval is not None else None
        self.account = BillingAccount(account_id=ACC, iam_policy={USER: "owner"} if owned else {}, account_defaults=defaults)

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return self.account if account_id == ACC else None


class Users:
    def __init__(self, interval: Optional[int] = None) -> None:
        config = UserBotConfig(timezone=TZ, fact_review_interval_days=interval)
        self.held = {USER: UserProfile(user_id=USER, account_id=ACC, config=config)}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self.held.get(user_id)


def make(world: ReviewWorld, *, dry_run: bool = False, queue: bool = True, owned: bool = True,
         user_interval: Optional[int] = None, account_interval: Optional[int] = None) -> FactLifecycleSweepService:
    accounts, users = Accounts(owned, account_interval), Users(user_interval)
    return FactLifecycleSweepService(
        world.repo, world.counters, world.sink, dry_run, clock=world.clock,
        review_store=world.review_store, task_queue=world.queue if queue else None,
        owner_zones=OwnerTimezoneResolver(accounts, users), account_repo=accounts, user_repo=users,
    )


async def run(world: ReviewWorld, **kwargs: Any) -> Dict[str, Any]:
    return await make(world, **kwargs).sweep()


def daily(world: ReviewWorld, account: str = ACC) -> Dict[str, Any]:
    return world.store.db.collection(world.env.fact_lifecycle_daily_collection).docs[f"{account}:{world.now.date().isoformat()}"]


def daily_ids(world: ReviewWorld) -> set:
    """The ids of today's rows (the counters written for yesterday are rows of their own)."""
    today = world.now.date().isoformat()
    return {doc_id for doc_id in world.store.db.collection(world.env.fact_lifecycle_daily_collection).docs if doc_id.endswith(today)}


async def reviewed_recently(world: ReviewWorld, **over: Any) -> None:
    """The account's review state, its last review an hour ago: the breaker's age is fine, the interval has not elapsed."""
    over.setdefault("last_review_at", world.now - timedelta(hours=1))
    await world.seed_state(account_id=ACC, **over)


async def due(world: ReviewWorld, text: str, **over: Any) -> str:
    """A current fact whose expiry passed two days ago."""
    over.setdefault("expires_at", PAST)
    return await world.seed(text, **over)


async def ask(world: ReviewWorld, question_id: str, fact_id: str, status: QuestionStatus, **over: Any):
    return await world.seed_question(status, question_id=question_id, account_id=ACC, user_id=USER, fact_id=fact_id,
                                     quoted_text=world.docs[fact_id]["text"], **over)


def expected_name(world: ReviewWorld, key: str) -> str:
    return world.queue.client.task_path(
        world.queue.project_id, world.queue.location, world.queue.queue_name,
        f"{FACT_REVIEW_TASK}-{hashlib.sha256(key.encode()).hexdigest()[:32]}",
    )


def review_tasks(world: ReviewWorld) -> list:
    return [(request["task"]["name"], body) for request, body in zip(
        world.tasks.create_task_requests, world.tasks.created_bodies()) if body["task_type"] == FACT_REVIEW_TASK]


def alerts(world: ReviewWorld) -> list:
    return [text for text in world.sink.texts if text.startswith("🚨")]


# ------------------------------------------------------------------------------------------------ accounts visited

async def test_it_visits_every_account_the_review_has_to_do_with_and_no_other():
    world = ReviewWorld()
    yesterday = world.now.date() - timedelta(days=1)
    await due(world, "Alex has a dentist appointment")                                        # ACC: a due fact
    await world.seed_state(account_id="acc-3", undigested_since=world.now - timedelta(days=1))    # a state, nothing else
    other_fact = await world.seed("Clara waters the plants", account=OTHER, expires_at=PAST + timedelta(days=60))
    await world.seed_question(QuestionStatus.QUEUED, question_id="qo", account_id=OTHER, user_id="usr-2", fact_id=other_fact,
                              quoted_text="Clara waters the plants")                           # OTHER: an open question only
    await world.counters.add("acc-4", yesterday, {CounterField.REPLY_INTAKE_FAILED: 1})      # a review figure yesterday
    await world.counters.add("acc-5", yesterday, {CounterField.EXPIRY_TOOL_ERRORS: 2})       # not a review figure

    result = await run(world)

    assert daily_ids(world) == {f"{account}:{world.now.date().isoformat()}" for account in (ACC, "acc-3", OTHER, "acc-4")}
    assert result["accounts"] == 4


async def test_the_result_carries_the_two_new_figures_and_the_snapshot_only_the_first():
    world = ReviewWorld()
    await world.seed_state(account_id=ACC, undigested_since=world.now - timedelta(days=1))

    result = await run(world)

    assert (result["review_enqueued"], result["purge_overdue"]) == (1, 0)
    assert "purge_overdue" not in {f.name for f in fields(SweepSnapshot)}
    assert "purge_overdue" not in daily(world) and daily(world)["review_enqueued"] == 1


# ------------------------------------------------------------------------------------------------ open questions

@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "log_only"])
@pytest.mark.parametrize("status", OPEN)
async def test_a_due_fact_of_an_open_question_is_neither_staled_nor_counted_and_shows_as_waiting(status, dry_run):
    world = ReviewWorld()
    await reviewed_recently(world)
    waiting = await due(world, "Alex plans a weekend in Coimbra")
    free = await due(world, "Alex has a dentist appointment")
    await ask(world, "q1", waiting, status)

    result = await run(world, dry_run=dry_run)

    assert world.docs[waiting]["state"] == "current" and world.docs[waiting]["stale_since"] is None
    snap = daily(world)
    assert (snap["due"], snap["deferred"], snap["no_decision"], snap["breaker"]) == (1, 0, 0, "ok")
    assert (result["due"], result["deferred"]) == (1, 0)
    if dry_run:
        assert world.docs[free]["state"] == "current" and (snap["staled"], snap["would_stale"]) == (0, 1)
    else:
        assert world.docs[free]["state"] == "stale" and (snap["staled"], snap["would_stale"]) == (1, 0)
    assert "due_waiting 1" in world.sink.texts[0] and "due 1 " in world.sink.texts[0]


async def test_a_line_with_no_waiting_fact_has_no_waiting_segment():
    world = ReviewWorld()
    await reviewed_recently(world)
    await due(world, "Alex has a dentist appointment")

    await run(world)

    assert "due_waiting" not in world.sink.texts[0]


async def test_a_due_fact_of_a_closed_question_is_staled():
    world = ReviewWorld()
    await reviewed_recently(world)
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.CLOSED, asked_at=world.now - timedelta(days=5))

    await run(world)

    assert world.docs[fact]["state"] == "stale" and daily(world)["staled"] == 1
    assert "due_waiting" not in world.sink.texts[0]


# ------------------------------------------------------------------------------------------------ the pool and the cap

async def seed_queued(world: ReviewWorld, count: int) -> None:
    for i in range(count):
        fact = await due(world, f"Alex follows the plan number {i:02d}", expires_at=PAST - timedelta(days=i))
        await ask(world, f"q{i:02d}", fact, QuestionStatus.QUEUED)


async def test_the_cap_counts_the_facts_of_queued_questions_45_queued_and_5_stale_stale_nothing():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 45)
    for i in range(5):
        await world.seed_stale(f"Clara kept a diary entry {i}")
    free = [await due(world, f"Clara has errand {i}", expires_at=PAST - timedelta(days=100 + i)) for i in range(2)]

    result = await run(world)

    assert [world.docs[f]["state"] for f in free] == ["current", "current"]
    snap = daily(world)
    assert (snap["staled"], snap["deferred"], snap["breaker"], snap["due"]) == (0, 2, "ok", 2)
    assert (result["staled"], result["breaker_tripped"]) == (0, 0)
    assert "due_waiting 45" in world.sink.texts[0]


async def test_one_fewer_queued_question_leaves_headroom_for_exactly_one_fact_the_oldest():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 44)
    for i in range(5):
        await world.seed_stale(f"Clara kept a diary entry {i}")
    oldest = await due(world, "Clara has the oldest errand", expires_at=PAST - timedelta(days=200))
    newer = await due(world, "Clara has a newer errand", expires_at=PAST - timedelta(days=100))

    await run(world)

    assert (world.docs[oldest]["state"], world.docs[newer]["state"]) == ("stale", "current")
    assert (daily(world)["staled"], daily(world)["deferred"]) == (1, 1)


async def test_log_only_reports_the_same_cap_over_the_pool():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 45)
    for i in range(5):
        await world.seed_stale(f"Clara kept a diary entry {i}")
    for i in range(2):
        await due(world, f"Clara has errand {i}", expires_at=PAST - timedelta(days=100 + i))

    await run(world, dry_run=True)

    assert (daily(world)["would_stale"], daily(world)["deferred"], daily(world)["staled"]) == (2, 2, 0)


# ------------------------------------------------------------------------------------------------ the breaker

@pytest.mark.parametrize("user_interval, account_interval, days", [
    (None, None, 1), (3, None, 3), (None, 3, 3), (2, 3, 2),
], ids=["default_1", "user_3", "account_3", "user_2_over_account_3"])
async def test_the_breaker_trips_when_the_last_review_is_older_than_the_interval_plus_72_hours(
    user_interval, account_interval, days
):
    limit = timedelta(days=days, hours=72)
    at_the_edge = ReviewWorld()
    await at_the_edge.seed_state(account_id=ACC, last_review_at=at_the_edge.now - limit)
    await at_the_edge.seed_stale("Clara kept a diary entry")
    kept = await due(at_the_edge, "Alex has a dentist appointment")
    await run(at_the_edge, user_interval=user_interval, account_interval=account_interval)
    assert at_the_edge.docs[kept]["state"] == "stale" and daily(at_the_edge)["breaker"] == "ok"

    past_it = ReviewWorld()
    await past_it.seed_state(account_id=ACC, last_review_at=past_it.now - limit - timedelta(seconds=1))
    await past_it.seed_stale("Clara kept a diary entry")
    held = await due(past_it, "Alex has a dentist appointment")
    result = await run(past_it, user_interval=user_interval, account_interval=account_interval)

    assert past_it.docs[held]["state"] == "current" and daily(past_it)["breaker"] == "tripped"
    assert (daily(past_it)["staled"], result["breaker_tripped"]) == (0, 1)
    assert f"no successful review in the last {days * 24 + 72} h" in alerts(past_it)[0]


async def test_a_pool_over_50_of_stale_facts_alone_trips_the_breaker():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(51):
        await world.seed_stale(f"Clara kept a diary entry {i:02d}")
    held = await due(world, "Alex has a dentist appointment")

    await run(world)

    assert world.docs[held]["state"] == "current" and daily(world)["breaker"] == "tripped"
    assert "pool of 51" in alerts(world)[0]


async def test_stale_facts_and_facts_of_open_questions_make_one_pool_for_the_breaker():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(30):
        await world.seed_stale(f"Clara kept a diary entry {i:02d}")
    await seed_queued(world, 21)
    held = await due(world, "Alex has a dentist appointment", expires_at=PAST - timedelta(days=300))

    await run(world)

    assert world.docs[held]["state"] == "current" and daily(world)["breaker"] == "tripped"
    assert "pool of 51" in alerts(world)[0]


async def test_a_pool_of_exactly_50_does_not_trip_but_leaves_no_headroom():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(30):
        await world.seed_stale(f"Clara kept a diary entry {i:02d}")
    await seed_queued(world, 20)
    held = await due(world, "Alex has a dentist appointment", expires_at=PAST - timedelta(days=300))

    await run(world)

    assert world.docs[held]["state"] == "current" and daily(world)["breaker"] == "ok"
    assert (daily(world)["staled"], daily(world)["deferred"]) == (0, 1)


# ------------------------------------------------------------------------------------------------ the daily review task

async def seed_stale_reason(world: ReviewWorld) -> None:
    await world.seed_state(account_id=ACC, last_review_at=world.now - timedelta(days=2))
    await world.seed_stale("Clara kept a diary entry")


async def seed_reply_reason(world: ReviewWorld) -> None:
    await reviewed_recently(world)
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.ASKED, asked_at=world.now - timedelta(hours=3),
              reply_text="Not any more", reply_at=world.now - timedelta(hours=1))


async def seed_timeout_reason(world: ReviewWorld) -> None:
    await reviewed_recently(world)
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.UNANSWERED, asked_at=world.now - timedelta(days=15))


async def seed_pending_reason(world: ReviewWorld) -> None:
    await reviewed_recently(world)
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.PENDING)


async def seed_digest_reason(world: ReviewWorld) -> None:
    await reviewed_recently(world, undigested_since=world.now - timedelta(days=2))     # no fact, no question: a state only


async def seed_queue_reason(world: ReviewWorld) -> None:
    await reviewed_recently(world)
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.QUEUED)


REASONS = [seed_stale_reason, seed_reply_reason, seed_timeout_reason, seed_pending_reason, seed_digest_reason,
           seed_queue_reason]


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "log_only"])
@pytest.mark.parametrize("seed", REASONS, ids=[s.__name__.removeprefix("seed_").removesuffix("_reason") for s in REASONS])
async def test_each_reason_enqueues_one_daily_review_for_the_owner_in_both_modes(seed, dry_run):
    world = ReviewWorld()
    await seed(world)

    result = await run(world, dry_run=dry_run)

    name, body = review_tasks(world)[0]
    assert len(review_tasks(world)) == 1
    assert body == {"task_type": "fact_review", "user_id": USER, "account_id": ACC, "mode": "daily"}
    assert name == expected_name(world, "fact-review:acc-1:2026-10-23")
    assert world.tasks.create_task_requests[0]["task"]["dispatch_deadline"].seconds == 1800
    assert (result["review_enqueued"], daily(world)["review_enqueued"]) == (1, 1)
    state = world.state_doc(ACC)
    assert state["enqueued_at"] == NOW and state["unsuccessful_since"] == NOW


async def test_a_digest_only_account_with_no_fact_is_visited_and_enqueued():
    world = ReviewWorld()
    await seed_digest_reason(world)
    assert world.docs == {}

    result = await run(world)

    assert (result["accounts"], result["review_enqueued"]) == (1, 1)


async def test_nothing_to_do_enqueues_nothing_and_writes_no_mark():
    world = ReviewWorld()
    await reviewed_recently(world)                                    # reviewed an hour ago, nothing stale or open
    await world.seed_stale("Clara kept a diary entry")                 # a stale fact, but the interval has not elapsed

    result = await run(world)

    assert review_tasks(world) == [] and result["review_enqueued"] == 0 and daily(world)["review_enqueued"] == 0
    assert world.state_doc(ACC).get("enqueued_at") is None


@pytest.mark.parametrize("user_interval, last_review_days_ago, enqueued", [(3, 2, 0), (3, 3, 1), (1, 1, 1)])
async def test_the_interval_is_counted_in_owner_local_days(user_interval, last_review_days_ago, enqueued):
    world = ReviewWorld()
    await world.seed_state(account_id=ACC, last_review_at=world.now - timedelta(days=last_review_days_ago, hours=-1))
    await world.seed_stale("Clara kept a diary entry")

    result = await run(world, user_interval=user_interval)

    assert result["review_enqueued"] == enqueued and len(review_tasks(world)) == enqueued


async def test_a_queued_question_goes_out_when_the_current_one_was_sent_on_an_earlier_local_day():
    world = ReviewWorld()
    sent = await due(world, "Clara has errand one")
    waiting = await due(world, "Clara has errand two")
    await ask(world, "q-sent", sent, QuestionStatus.ASKED, asked_at=world.now - timedelta(days=1))
    await ask(world, "q-wait", waiting, QuestionStatus.QUEUED)
    await reviewed_recently(world, current_question_id="q-sent", sent_on=(world.now - timedelta(days=1)).date(), sent=1)

    assert (await run(world))["review_enqueued"] == 1


async def test_a_queued_question_waits_while_the_current_one_was_sent_today():
    world = ReviewWorld()
    sent = await due(world, "Clara has errand one")
    waiting = await due(world, "Clara has errand two")
    await ask(world, "q-sent", sent, QuestionStatus.ASKED, asked_at=world.now - timedelta(hours=1))
    await ask(world, "q-wait", waiting, QuestionStatus.QUEUED)
    await reviewed_recently(world, current_question_id="q-sent", sent_on=world.now.date(), sent=1)

    assert (await run(world))["review_enqueued"] == 0


# ------------------------------------------------------------------------------------------------ once per local day

async def test_two_runs_on_one_local_day_make_one_task_and_both_report_the_review_requested():
    world = ReviewWorld()
    await seed_digest_reason(world)
    janitor = make(world)

    first = await janitor.sweep()
    world.advance(hours=1)
    second = await janitor.sweep()

    assert len(review_tasks(world)) == 1
    assert (first["review_enqueued"], second["review_enqueued"]) == (1, 1)
    assert daily(world)["review_enqueued"] == 1


async def test_a_run_at_23_58_and_one_at_00_03_local_enqueue_twice():
    world = ReviewWorld()
    world.now = datetime(2026, 10, 23, 22, 58, tzinfo=timezone.utc)        # 23:58 on the 23rd in Lisbon
    await seed_digest_reason(world)
    janitor = make(world)

    await janitor.sweep()
    world.now = datetime(2026, 10, 23, 23, 3, tzinfo=timezone.utc)         # 00:03 on the 24th in Lisbon, the same UTC day
    await janitor.sweep()

    assert [name for name, _ in review_tasks(world)] == [
        expected_name(world, daily_key(ACC, datetime(2026, 10, 23, 12, tzinfo=timezone.utc), TZ)),
        expected_name(world, daily_key(ACC, datetime(2026, 10, 24, 12, tzinfo=timezone.utc), TZ)),
    ]
    assert expected_name(world, "fact-review:acc-1:2026-10-23") != expected_name(world, "fact-review:acc-1:2026-10-24")


# ------------------------------------------------------------------------------------------------ nobody to enqueue for

async def test_an_account_with_no_owner_is_warned_about_and_nothing_is_enqueued(caplog):
    caplog.set_level(logging.WARNING)
    world = ReviewWorld()
    await seed_digest_reason(world)

    result = await run(world, owned=False)

    assert review_tasks(world) == [] and result["review_enqueued"] == 0 and daily(world)["review_enqueued"] == 0
    assert world.state_doc(ACC).get("enqueued_at") is None
    assert any("has no owner; not enqueued" in r.getMessage() for r in caplog.records)


async def test_a_missing_task_queue_is_warned_about_and_the_run_goes_on(caplog):
    caplog.set_level(logging.WARNING)
    world = ReviewWorld()
    await seed_digest_reason(world)
    fact = await due(world, "Alex has a dentist appointment")

    result = await run(world, queue=False)

    assert result["review_enqueued"] == 0 and daily(world)["review_enqueued"] == 0
    assert world.state_doc(ACC).get("enqueued_at") is None and world.tasks.create_task_requests == []
    assert world.docs[fact]["state"] == "stale"                                          # the staling is not held back
    assert any("there is no task queue; not enqueued" in r.getMessage() for r in caplog.records)


async def test_the_per_account_line_names_whether_a_review_was_enqueued(caplog):
    caplog.set_level(logging.INFO)
    world = ReviewWorld()
    await seed_digest_reason(world)

    await run(world)

    assert any(m.endswith("review_enqueued=1 dry_run=False") for m in (r.getMessage() for r in caplog.records)
               if m.startswith("[FactLifecycle] account="))


# ------------------------------------------------------------------------------------------------ the Cabinet and the review

async def test_a_question_whose_fact_the_cabinet_invalidated_is_reached_and_the_next_review_closes_it():
    world = ReviewWorld()
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.UNANSWERED, asked_at=world.now - timedelta(days=15))
    world.docs[fact]["state"] = "invalidated"                              # the owner deleted it in the Cabinet
    assert world.state_doc(ACC) is None                                    # the account has no review state: only the question

    result = await run(world)

    assert (result["accounts"], result["review_enqueued"]) == (1, 1)
    (_, body), = review_tasks(world)
    rig = Rig(world)
    assert await rig.service.run(body["user_id"], body["account_id"], body["mode"]) == {"status": "completed"}
    question = await stored_question(world, "q1")
    assert question.status is QuestionStatus.CLOSED and question.closed_by is ClosedBy.RESOLVED_ELSEWHERE
    assert world.docs[fact]["state"] == "invalidated"


# ------------------------------------------------------------------------------------------------ the purge check

async def closed_long_ago(world: ReviewWorld, question_id: str, days_ago: int) -> None:
    opened = build_question(
        QuestionStatus.ASKED, question_id=question_id, account_id=ACC, user_id=USER, fact_id="f-old",
        queued_at=world.now - timedelta(days=days_ago + 10), queue_key=world.now - timedelta(days=days_ago + 20),
        asked_at=world.now - timedelta(days=days_ago + 5),
    )
    await world.put_question(opened.closed(ClosedBy.TIMEOUT, world.now - timedelta(days=days_ago)))


async def test_a_closed_question_kept_more_than_three_days_past_its_expiry_is_counted_in_the_result_only():
    world = ReviewWorld()
    await closed_long_ago(world, "q-overdue", 95)              # expires at 90 days: 5 days overdue
    await closed_long_ago(world, "q-recent", 91)              # 1 day overdue: inside the grace
    await closed_long_ago(world, "q-fresh", 10)

    result = await run(world)

    assert result["purge_overdue"] == 1
    assert all("purge_overdue" not in doc for doc in world.store.db.collection(world.env.fact_lifecycle_daily_collection).docs.values())


async def test_without_a_review_store_the_janitor_is_the_one_of_s3():
    world = ReviewWorld()
    await due(world, "Alex has a dentist appointment")

    result = await FactLifecycleSweepService(world.repo, world.counters, None, False, clock=world.clock).sweep()

    assert (result["staled"], result["review_enqueued"], result["purge_overdue"]) == (1, 0, 0)


def test_a_review_store_without_the_owner_reads_is_refused():
    world = ReviewWorld()
    with pytest.raises(ValueError, match="owner_zones"):
        FactLifecycleSweepService(world.repo, world.counters, None, False, review_store=world.review_store)
