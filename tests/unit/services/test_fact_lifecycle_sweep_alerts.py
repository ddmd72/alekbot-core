"""The janitor's alerts and the review's part of its ops line (Facts S4 · T9b, plan §3.11): one 🚨 per account per run, in
log-only mode too, each condition with its exclusions and repeat rule, the purge 🚨 and line, the line segments from
yesterday's counters. The real service on the review world, as in `test_fact_lifecycle_sweep_review.py`; assertions are on
the ops sink and the stored review state."""
from datetime import timedelta
from typing import List

import pytest

from src.domain.fact_lifecycle_day import CounterField
from src.domain.fact_review_question import QuestionStatus
from tests.support.fact_kit import ACC, OTHER
from tests.support.fact_review_world import ReviewWorld
from tests.unit.services.test_fact_lifecycle_sweep_review import (
    alerts, ask, closed_long_ago, due, reviewed_recently, run, seed_queued,
)


def lines(world: ReviewWorld) -> List[str]:
    return [text for text in world.sink.texts if text.startswith("🧹")]


def yesterday(world: ReviewWorld):
    return world.now.date() - timedelta(days=1)


async def stale_for(world: ReviewWorld, days: float, text: str = "Alex has a dentist appointment") -> str:
    return await world.seed(text, state="stale", stale_since=world.now - timedelta(days=days),
                            expires_at=world.now - timedelta(days=days + 1))


async def queue_state(world: ReviewWorld):
    return (world.state_doc(ACC) or {}).get("queue_alert_at")


# ------------------------------------------------------------------------------------------------ the pool

@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "log_only"])
async def test_a_pool_over_fifty_alerts_once_per_run_in_both_modes(dry_run):
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(51):
        await stale_for(world, 0.5, f"Clara kept a diary entry {i}")

    await run(world, dry_run=dry_run)

    (alert,) = alerts(world)
    assert ACC[:8] in alert and "51" in alert


async def test_a_pool_of_fifty_alerts_nothing():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(50):
        await stale_for(world, 0.5, f"Clara kept a diary entry {i}")

    await run(world)

    assert alerts(world) == []


async def test_the_pool_counts_the_facts_of_open_questions():
    world = ReviewWorld()
    await reviewed_recently(world)
    for i in range(30):
        await stale_for(world, 0.5, f"Clara kept a diary entry {i}")
    await seed_queued(world, 21)

    await run(world)

    (alert,) = alerts(world)
    assert "the pool is 51" in alert


# ------------------------------------------------------------------------------------------------ the oldest stale fact

@pytest.mark.parametrize("interval, silent_days, alert_days", [(1, 2.9, 3.1), (3, 4.9, 5.1)])
async def test_the_oldest_stale_fact_alerts_past_the_interval_plus_two_days(interval, silent_days, alert_days):
    quiet, loud = ReviewWorld(), ReviewWorld()
    for world, days in ((quiet, silent_days), (loud, alert_days)):
        await reviewed_recently(world)
        await stale_for(world, days)
        await run(world, account_interval=interval)

    assert alerts(quiet) == []
    (alert,) = alerts(loud)
    assert "the oldest stale fact" in alert and f"limit {interval + 2}" in alert


async def test_the_oldest_alert_is_recorded_and_not_repeated_within_a_week_but_then_repeats():
    world = ReviewWorld()
    await reviewed_recently(world)
    await stale_for(world, 10)

    await run(world)
    first = world.state_doc(ACC)["oldest_alert_at"]
    world.advance(days=6)
    await reviewed_recently(world, last_review_at=world.now - timedelta(hours=1), oldest_alert_at=first)
    await run(world)
    world.advance(days=1)
    await reviewed_recently(world, last_review_at=world.now - timedelta(hours=1), oldest_alert_at=first)
    await run(world)

    assert [("oldest stale fact" in alert) for alert in alerts(world)] == [True, True]
    assert first is not None


async def test_the_oldest_alert_is_in_log_only_mode_too():
    world = ReviewWorld()
    await reviewed_recently(world)
    await stale_for(world, 10)

    await run(world, dry_run=True)

    assert len(alerts(world)) == 1 and world.state_doc(ACC)["oldest_alert_at"] is not None


# ------------------------------------------------------------------------------------------------ the queue

async def test_the_queue_alerts_once_at_36_is_silent_at_37_rearms_at_35_and_alerts_again_at_36():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 36)

    await run(world)
    assert [("36 questions are queued" in a) for a in alerts(world)] == [True]
    assert await queue_state(world) is not None

    fact = await due(world, "Alex follows the plan number 99")
    await ask(world, "q99", fact, QuestionStatus.QUEUED)
    await run(world)
    assert len(alerts(world)) == 1                                      # 37 queued: silent, mark kept
    assert await queue_state(world) is not None

    await ask(world, "q99", fact, QuestionStatus.CLOSED, asked_at=world.now - timedelta(days=1))
    other = await due(world, "Alex follows the plan number 98")
    await ask(world, "q98", other, QuestionStatus.CLOSED, asked_at=world.now - timedelta(days=1))
    await ask(world, "q00", world.question_docs["q00"]["fact_id"], QuestionStatus.CLOSED, asked_at=world.now - timedelta(days=1))
    await run(world)                                                    # 35 queued: re-armed, nothing said
    assert len(alerts(world)) == 1 and await queue_state(world) is None

    third = await due(world, "Alex follows the plan number 97")
    await ask(world, "q97", third, QuestionStatus.QUEUED)
    await run(world)                                                    # 36 queued again
    assert len(alerts(world)) == 2 and await queue_state(world) is not None


async def test_the_queue_alert_is_there_in_log_only_mode():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 36)

    await run(world, dry_run=True)

    assert len(alerts(world)) == 1 and await queue_state(world) is not None


async def test_35_queued_questions_alert_nothing():
    world = ReviewWorld()
    await reviewed_recently(world)
    await seed_queued(world, 35)

    await run(world)

    assert alerts(world) == [] and await queue_state(world) is None


# ------------------------------------------------------------------------------------------------ 48 hours

async def test_unsuccessful_for_more_than_48_hours_alerts_and_repeats_every_run():
    world = ReviewWorld()
    await reviewed_recently(world, unsuccessful_since=world.now - timedelta(hours=49))

    await run(world)
    await run(world)

    assert [("no successful review for 49 h" in a) for a in alerts(world)] == [True, True]


async def test_unsuccessful_for_less_than_48_hours_alerts_nothing():
    world = ReviewWorld()
    await reviewed_recently(world, unsuccessful_since=world.now - timedelta(hours=47))

    await run(world)

    assert alerts(world) == []


# ------------------------------------------------------------------------------------------------ replies and expiry errors

async def test_yesterdays_reply_intake_failures_alert_and_show_on_the_line_but_todays_do_not():
    world = ReviewWorld()
    await reviewed_recently(world)
    await world.counters.add(ACC, yesterday(world), {CounterField.REPLY_INTAKE_FAILED: 2})
    await world.counters.add(OTHER, world.now.date(), {CounterField.REPLY_INTAKE_FAILED: 5})

    await run(world)

    (alert,) = alerts(world)
    assert ACC[:8] in alert and "2 owner replies were not recorded" in alert
    (line,) = lines(world)
    assert "replies not recorded 2" in line and OTHER[:8] not in line


@pytest.mark.parametrize("errors, expected", [(10, 0), (11, 1)])
async def test_expiry_tool_errors_alert_above_ten_only(errors, expected):
    world = ReviewWorld()
    await reviewed_recently(world)
    await due(world, "Alex has a dentist appointment")
    await world.counters.add(ACC, yesterday(world), {CounterField.EXPIRY_TOOL_ERRORS: errors})

    await run(world)

    assert len(alerts(world)) == expected


# ------------------------------------------------------------------------------------------------ one per account

async def test_every_condition_of_an_account_lands_in_one_message_and_another_account_gets_its_own():
    world = ReviewWorld()
    await reviewed_recently(world, unsuccessful_since=world.now - timedelta(days=3))
    for i in range(51):
        await stale_for(world, 10, f"Clara kept a diary entry {i}")
    await due(world, "Alex has a dentist appointment")
    await world.counters.add(ACC, yesterday(world), {CounterField.REPLY_INTAKE_FAILED: 1})
    await world.counters.add(ACC, yesterday(world), {CounterField.EXPIRY_TOOL_ERRORS: 12})
    other = await world.seed("Clara waters the plants", account=OTHER, state="stale", stale_since=world.now - timedelta(days=9),
                             expires_at=world.now - timedelta(days=10))
    assert other

    await run(world)

    mine = [a for a in alerts(world) if ACC[:8] in a]
    theirs = [a for a in alerts(world) if OTHER[:8] in a]
    assert len(mine) == 1 and len(alerts(world)) == len(mine) + len(theirs)
    for part in ("breaker tripped", "the pool is 51", "oldest stale fact", "no successful review", "not recorded",
                 "expiry tool calls were refused"):
        assert part in mine[0], part


async def test_a_breaker_alone_keeps_its_old_sentence():
    world = ReviewWorld()
    await stale_for(world, 0.5)                                   # a stale fact and no successful review ever: the breaker trips
    await due(world, "Alex plans a weekend in Coimbra")

    await run(world)

    (alert,) = alerts(world)
    assert alert.startswith(f"🚨 Fact lifecycle breaker tripped for account {ACC[:8]}:")
    assert alert.endswith("1 due facts were not staled.")


# ------------------------------------------------------------------------------------------------ the purge

async def test_a_purge_overdue_posts_a_run_level_alert_and_a_line_under_the_header_in_both_modes():
    for dry_run in (False, True):
        world = ReviewWorld()
        await closed_long_ago(world, "q-overdue", 95)

        result = await run(world, dry_run=dry_run)

        assert result["purge_overdue"] == 1
        (alert,) = alerts(world)
        assert "1 closed review records" in alert and ACC[:8] not in alert
        (line,) = lines(world)
        assert line.splitlines()[1] == "purge overdue 1"


async def test_no_overdue_record_means_no_purge_line_and_no_alert():
    world = ReviewWorld()
    await closed_long_ago(world, "q-fresh", 10)
    await due(world, "Alex has a dentist appointment")

    await run(world)

    assert alerts(world) == [] and "purge overdue" not in "".join(world.sink.texts)


# ------------------------------------------------------------------------------------------------ the line

async def test_the_review_segments_come_from_yesterdays_counters_and_todays_questions():
    world = ReviewWorld()
    await reviewed_recently(world)
    await world.counters.add(ACC, yesterday(world), {
        CounterField.REVIEWED: 4, CounterField.REVIVED: 1, CounterField.KEPT: 2, CounterField.QUEUED: 1,
        CounterField.QUESTIONS_SENT: 3, CounterField.ANSWERED_BY_DIGIT: 1, CounterField.TIMED_OUT: 1,
    })
    fact = await due(world, "Alex plans a weekend in Coimbra")
    await ask(world, "q1", fact, QuestionStatus.QUEUED)
    await ask(world, "q2", await due(world, "Alex has a dentist appointment"), QuestionStatus.UNANSWERED,
              asked_at=world.now - timedelta(days=2))

    await run(world)

    (line,) = lines(world)
    account = line.splitlines()[1]
    assert "| review: reviewed 4; counted on their own: revived 1, rewritten 0, archived 0, invalidated 0, superseded 0, kept 2, queued 1" in account
    assert "| questions sent 3, answered by digit 1, by button 0, in the Cabinet 0, by reply 0, timed out 1, queue 1, unanswered 1, due_waiting 2" in account
    assert "review failed" not in account and "replies not recorded" not in account
    assert account.endswith("| enqueued 1") or account.endswith("| enqueued 0")


async def test_todays_counters_are_not_on_the_line():
    world = ReviewWorld()
    await reviewed_recently(world)
    await world.counters.add(ACC, world.now.date(), {CounterField.REVIEWED: 9})
    await due(world, "Alex has a dentist appointment")

    await run(world)

    assert "review:" not in lines(world)[0]


async def test_an_account_with_no_review_figure_has_no_review_segment():
    world = ReviewWorld()
    await reviewed_recently(world)
    await due(world, "Alex has a dentist appointment")

    await run(world)

    assert not any(marker in lines(world)[0] for marker in ("review:", "questions sent", "enqueued"))
