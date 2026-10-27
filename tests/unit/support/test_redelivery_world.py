"""The redelivery world's own tests (P15 part 2, card A): each helper does what the P15 redelivery tests rely on.

Outcomes only: what the channels received, what the queue created, what the claim store holds, what the handler answered.
"""
import asyncio
from datetime import timedelta

import pytest
from google.api_core.exceptions import DeadlineExceeded

from src.domain.agent import AgentStatus
from src.domain.messaging import RichContent
from src.domain.notification_kind import NotificationKind
from src.services import reminders_service
from tests.support.clock_instants import pin_datetime
from tests.support.redelivery_world import (
    ACCOUNT,
    T0,
    USER,
    Post,
    PreconditionFailed,
    RedeliveryWorld,
    Rendezvous,
    Twice,
    deliver,
    deliver_twice,
    deliver_with_retry,
    ok,
    require,
)

TEXT = "Time to call dad"
TABLE = RichContent(content_type="table", data={"title": "Activities", "headers": ["Activity"], "rows": [{"cells": ["Yoga"]}]},
                    fallback_text="Activities")


async def _notify(world: RedeliveryWorld):
    return await world.notifier.notify(USER, ACCOUNT, "A reminder fired.", kind=NotificationKind.REMINDER)


def _gone_note_payload(world: RedeliveryWorld) -> dict:
    return world.payload("execute_reminder", note_id="n1", user_id=USER, due_at=T0.isoformat())


async def test_a_success_turn_posts_its_text_once_to_the_stored_primary_channel():
    """Regression: the channel factory keyed wrongly, or the primary channel not read (the last-active one differs)."""
    world = RedeliveryWorld()
    await world.state.save(USER, "slack", "C2")
    await world.set_primary_channel()
    world.router.always(ok(TEXT))

    result = await _notify(world)

    assert result.delivered and result.sent
    assert world.posts == [Post("slack", "C1", "text", TEXT)]
    assert world.texts == [TEXT]
    assert len(world.router.messages) == 1


async def test_fail_post_fails_exactly_the_nth_post_once():
    """`fail_post(2)` on a text + rich turn: the text is posted, the rich post raises, and notify reports sent but not
    delivered. Regression: `fail_post` failing the wrong post (the retry-after-effect tests would then test nothing)."""
    world = RedeliveryWorld()
    await world.set_primary_channel()
    world.router.always(ok(TEXT, rich=TABLE))
    world.channels.fail_post(2)

    result = await _notify(world)

    assert (result.delivered, result.sent, result.agent_status) == (False, True, AgentStatus.FAILED)
    assert world.texts == [TEXT]
    assert [p.kind for p in world.posts] == ["text"]

    again = await _notify(world)    # the failure was used up

    assert again.delivered
    assert [p.kind for p in world.posts] == ["text", "text", "rich"]


async def test_an_enqueuing_turn_creates_one_unnamed_agent_task_through_the_real_queue():
    """Regression: the router naming the task (a name would let Cloud Tasks refuse a re-run's dispatch and hide P15-D1)."""
    world = RedeliveryWorld()
    await world.set_primary_channel()
    world.router.always(ok(TEXT, enqueue=("create_html_page",)))

    await _notify(world)

    bodies = world.bodies()
    assert len(bodies) == 1
    assert (bodies[0]["task_type"], bodies[0]["intent"]) == ("agent_execution", "create_html_page")
    assert bodies[0]["context"] == {"user_id": USER, "account_id": ACCOUNT}
    assert "name" not in world.client.create_task_requests[0]["task"]
    assert world.bodies("agent_execution") == bodies and world.bodies("execute_reminder") == []


async def test_one_task_name_delivered_twice_is_a_duplicate_with_one_done_claim():
    world = RedeliveryWorld()
    p = _gone_note_payload(world)

    first = await deliver(world.handler, p, "t-1")
    second = await deliver(world.handler, p, "t-1")

    assert first == ({"status": "note_gone"}, 200)
    assert second == ({"status": "duplicate"}, 200)
    assert [d["state"] for d in world.claim_docs] == ["done"]


async def test_two_task_names_both_run():
    world = RedeliveryWorld()
    p = _gone_note_payload(world)

    answers = [await deliver(world.handler, p, "t-1"), await deliver(world.handler, p, "t-2")]

    assert answers == [({"status": "note_gone"}, 200)] * 2
    assert [d["state"] for d in world.claim_docs] == ["done", "done"]


async def test_fail_next_raises_before_or_after_the_task_landed():
    world = RedeliveryWorld()
    body = {"note_id": "n1", "user_id": USER, "due_at": T0.isoformat()}

    world.client.fail_next(landed=False)
    with pytest.raises(DeadlineExceeded):
        await world.dispatch.enqueue_worker_task("execute_reminder", body)
    assert world.bodies() == [] and world.client.failed_attempts == 1

    world.client.fail_next(landed=True)
    with pytest.raises(DeadlineExceeded):
        await world.dispatch.enqueue_worker_task("execute_reminder", body)
    assert world.bodies() == [{"task_type": "execute_reminder", **body}] and world.client.failed_attempts == 2

    await world.dispatch.enqueue_worker_task("execute_reminder", body)
    assert len(world.bodies("execute_reminder")) == 2 and world.client.failed_attempts == 2


async def test_deliver_with_retry_redelivers_after_a_500_and_a_raise_and_not_after_a_200(monkeypatch):
    world = RedeliveryWorld()
    world.router.always(ok(TEXT))

    # No channel: notify reports no_channel, the handler answers 500, the claim is released and the task comes back.
    claimed = await world.seed_note(monkeypatch, due=T0, last_fired=T0 + timedelta(seconds=30),
                                    created_at=T0 - timedelta(days=1))
    p = world.payload("execute_reminder", note_id=claimed, user_id=USER, due_at=T0.isoformat())
    no_channel = await deliver_with_retry(world.handler, p, "t-500")
    assert [o[1] for o in no_channel] == [500, 500]
    assert all(o[0]["error"] == "no_channel" for o in no_channel)
    assert world.router.messages == [] and world.claim_docs == []

    # A gone note: 200, acked, no second delivery.
    gone = await deliver_with_retry(world.handler, _gone_note_payload(world), "t-200")
    assert gone == [({"status": "note_gone"}, 200)]

    # A tick whose enqueue raises: the raise is redelivered under the same name.
    await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1, seconds=1))
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
    world.client.fail_next(landed=False)
    tick = await deliver_with_retry(world.handler, world.payload("fire_due_reminders"), "t-raise")
    assert len(tick) == 2
    assert isinstance(tick[0], DeadlineExceeded)
    assert isinstance(tick[1], tuple) and tick[1][1] == 200


async def test_two_gathered_callers_meet_at_the_rendezvous():
    rv = Rendezvous()

    async def caller() -> str:
        await rv.arrive()
        return "passed"

    assert await asyncio.gather(caller(), caller()) == ["passed", "passed"]
    assert rv.met and rv.reached.is_set() and rv.arrivals == 2


async def test_a_lone_caller_is_released_when_the_other_ends_and_counts_once():
    rv = Rendezvous()

    async def arrives_twice() -> None:
        await rv.arrive()
        await rv.arrive()   # already arrived: passes straight through

    lone = asyncio.create_task(arrives_twice())
    await asyncio.sleep(0)
    assert rv.reached.is_set() and not lone.done()

    rv.done()    # the other party ended without reaching the point
    await asyncio.wait_for(lone, 1)

    assert rv.arrivals == 1 and not rv.met


async def test_deliver_twice_gathered_overlaps_at_a_wrapped_read():
    world = RedeliveryWorld()
    rv = Rendezvous()
    rv.wrap(world.notes, "get_note")

    twice = await deliver_twice(world.handler, _gone_note_payload(world), gathered=True, rendezvous=rv)

    assert twice.overlapped and rv.met and not rv.stalled
    assert twice.raised == [] and twice.statuses == [200, 200]
    assert twice.answers == [({"status": "note_gone"}, 200)] * 2
    assert [d["state"] for d in world.claim_docs] == ["done", "done"]


async def test_deliver_twice_in_sequence_does_not_overlap_and_does_not_wait():
    world = RedeliveryWorld()
    rv = Rendezvous()
    rv.wrap(world.notes, "get_note")

    twice = await asyncio.wait_for(
        deliver_twice(world.handler, _gone_note_payload(world), gathered=False, rendezvous=rv), 1)

    assert twice.overlapped is False and rv.met is False and rv.arrivals == 2 and not rv.stalled
    assert twice.answers == [({"status": "note_gone"}, 200)] * 2


async def test_two_scheduler_bodies_in_sequence_pass_a_rendezvous_without_waiting():
    world = RedeliveryWorld()
    rv = Rendezvous()
    rv.wrap(world.notes, "get_note")

    twice = await asyncio.wait_for(
        deliver_twice(world.handler, _gone_note_payload(world), gathered=False, names=None, rendezvous=rv), 1)

    assert twice.overlapped is False and rv.met is False and not rv.stalled
    assert twice.statuses == [200, 200]


async def test_deliver_twice_without_names_sends_scheduler_bodies_that_are_never_claimed(monkeypatch):
    world = RedeliveryWorld()
    deliveries = []
    handle = world.handler.handle

    async def recording(payload, delivery=None, headers=None):
        deliveries.append(delivery)
        return await handle(payload, delivery=delivery, headers=headers)

    monkeypatch.setattr(world.handler, "handle", recording)

    twice = await deliver_twice(world.handler, _gone_note_payload(world), gathered=False, names=None)

    assert deliveries == [None, None]
    assert twice.answers == [({"status": "note_gone"}, 200)] * 2
    assert world.db.collection(world.env.worker_task_dedup_collection).docs == {}


async def test_twice_reports_a_raised_delivery_by_its_exception(monkeypatch):
    """A tick whose enqueue raises: `raised` holds the exception, `statuses` has None in its place."""
    world = RedeliveryWorld()
    await world.seed_note(monkeypatch, due=T0, created_at=T0 - timedelta(days=1))
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
    world.client.fail_next(landed=False)

    twice = await deliver_twice(world.handler, world.payload("fire_due_reminders"), gathered=False, names=None)

    assert len(twice.raised) == 1 and isinstance(twice.raised[0], DeadlineExceeded)
    assert twice.statuses == [None, 200]


async def test_a_lone_caller_stalls_after_the_bound_and_passes_through():
    rv = Rendezvous(bound_s=0.05)

    await asyncio.wait_for(rv.arrive(), 1)

    assert rv.stalled and not rv.met and rv.reached.is_set()


async def test_a_fix_that_serialises_the_deliveries_stalls_the_rendezvous_instead_of_hanging():
    """Models a future fix that holds a lock across the race point: the second delivery waits on the lock, never
    arrives; the first is released by the bound, and `met` stays False, so `require(rv.met and not rv.stalled, ...)`
    reports FAILED. `overlapped` is still True: alone it does not prove the deliveries met."""
    world = RedeliveryWorld()
    rv = Rendezvous(bound_s=0.05)
    rv.wrap(world.notes, "get_note")
    at_the_point = world.notes.get_note
    lock = asyncio.Lock()

    async def serialised(*args, **kwargs):
        async with lock:
            return await at_the_point(*args, **kwargs)

    world.notes.get_note = serialised   # type: ignore[method-assign]

    twice = await asyncio.wait_for(
        deliver_twice(world.handler, _gone_note_payload(world), gathered=True, rendezvous=rv), 1)

    assert rv.stalled and not rv.met
    assert twice.overlapped
    assert twice.statuses == [200, 200] and twice.raised == []
    with pytest.raises(PreconditionFailed, match="met"):
        require(rv.met and not rv.stalled, "the deliveries met at the point")


def test_payload_refuses_a_missing_required_key_and_an_unknown_key():
    world = RedeliveryWorld()

    with pytest.raises(ValueError, match="due_at"):
        world.payload("execute_reminder", note_id="n1", user_id=USER)
    with pytest.raises(ValueError, match="colour"):
        world.payload("execute_reminder", note_id="n1", user_id=USER, due_at=T0.isoformat(), colour="green")
    assert world.payload("daily_email_review", user_id=USER, account_id=ACCOUNT) == {
        "task_type": "daily_email_review", "user_id": USER, "account_id": ACCOUNT}


# ----------------------------------------------------------------------------- preconditions of a strict xfail

def test_a_failed_require_is_not_the_expected_failure_of_a_strict_xfail():
    """`xfail(strict=True, raises=AssertionError)` takes only an `AssertionError` as the expected failure: a failed
    `require` raises `PreconditionFailed`, which is not one, so the test reports FAILED."""
    assert not issubclass(PreconditionFailed, AssertionError)
    require(True, "holds")
    with pytest.raises(PreconditionFailed, match="the effect happened") as raised:
        require(False, "the effect happened")
    assert not isinstance(raised.value, AssertionError)


def test_reraise_raises_the_first_raised_delivery_as_itself_and_is_quiet_without_one():
    broken = TypeError("broken setup")
    with pytest.raises(TypeError) as raised:
        Twice(answers=[({"status": "ok"}, 200), broken, RuntimeError("second")], overlapped=False).reraise()
    assert raised.value is broken

    Twice(answers=[({"status": "ok"}, 200), ({"status": "ok"}, 200)], overlapped=True).reraise()


def test_reraise_wraps_a_delivery_assertion_error_so_it_is_not_the_expected_failure():
    """A fake's `AssertionError` (an unsupported operation: a broken setup) comes out as `PreconditionFailed`."""
    unsupported = AssertionError("unsupported operator")
    with pytest.raises(PreconditionFailed, match="unsupported operator") as raised:
        Twice(answers=[unsupported, ({"status": "ok"}, 200)], overlapped=True).reraise()
    assert raised.value.__cause__ is unsupported
