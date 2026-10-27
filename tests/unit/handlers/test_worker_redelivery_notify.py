"""A reminder fire delivered twice posts once (P15 part 2, card D), on the redelivery world.

The one notify-bearing type that is neither run twice by design nor already pinned: `execute_reminder`. Both tests deliver
the same fire under two task names the claim does not merge (`t-1`, `t-2`), through the real note store, the real
`UserNotificationService` and the real `WorkerHandler`; the second delivery must find the first one's work in the store.

Gathered is unreachable for this type: one task per fire (the fire is claimed, then enqueued once,
`src/services/reminders_service.py:151-181`), and a concurrent copy under one name is refused in-process
(`src/handlers/worker_handler.py:215-221`, pinned by `tests/unit/test_worker_redelivery_route.py::test_a_gathered_double_runs_once`).
Two distinct names never arise for one fire. The sequential pair `t-1`, `t-2` stands for a later delivery of the same fire
that the claim lets through: a retry that takes over a dead or expired claim
(`src/adapters/firestore_task_claim_store.py:39-45,61-74`). What the second delivery then finds is the first one's work
in the store.

Dropped from rev 1 of the card (each by design, or already pinned):
- `daily_email_review` "two distinct deliveries run twice": by design. The per-date mark `daily_review:{user}:{date}` is
  claimed before the enqueue and names the task (`src/handlers/worker_handler.py:1117-1128,1141`); two distinct deliveries
  of one body are two reviews.
- `agent_execution` "two distinct deliveries run twice": by design. A body is one agent run (`src/handlers/worker_handler.py:293`,
  then `AgentWorkerHandler`); two distinct tasks are two runs.
- the video `done` poll "delivers twice": by design. `src/handlers/worker_handler.py:788-800` delivers on `done`; a second
  distinct `done` poll delivers again, and no regression would fail such a test.
- `deep_research_failed` producer twice: already pinned, `tests/unit/test_deep_research_failed_notice.py:113` asserts the
  job-keyed `dedup_key`.
"""
from datetime import timedelta

from tests.support.redelivery_world import T0, USER, RedeliveryWorld, deliver_twice, ok

TEXT = "Time to call dad"


async def test_a_one_time_reminder_delivered_twice_posts_once_and_the_second_finds_it_gone(monkeypatch):
    """The first delivery posts, stamps and deletes the fired one-time note; the second finds no note and acks.

    Regression: the delete after delivery dropped (`src/handlers/worker_handler.py:1044-1047`): the second delivery then
    answers `already_delivered` (and does the delete itself, `:970-972`) instead of `note_gone`. The answer, not the
    store, catches it."""
    world = RedeliveryWorld()
    await world.set_primary_channel()
    world.router.always(ok(TEXT))
    # A claimed fire, as `RemindersService.fire_due_reminders` leaves it: `last_fired` written, the note kept for the delivery.
    note_id = await world.seed_note(monkeypatch, due=T0, last_fired=T0 + timedelta(seconds=30),
                                    created_at=T0 - timedelta(days=1))
    assert note_id in world.note_docs
    payload = world.payload("execute_reminder", note_id=note_id, user_id=USER, due_at=T0.isoformat())

    twice = await deliver_twice(world.handler, payload, gathered=False)

    assert twice.answers == [({"status": "ok"}, 200), ({"status": "note_gone"}, 200)]
    assert world.texts == [TEXT]
    assert len(world.router.messages) == 1
    assert note_id not in world.note_docs


async def test_a_recurrent_fire_delivered_twice_posts_once_through_the_stored_stamp(monkeypatch):
    """The stamp `last_delivered_due` goes through the store and comes back equal to the fire's `due_at`; the second
    delivery of the same fire answers `already_delivered` and the note stays for its next fire.

    The note is as the claim leaves a recurrent one: `due` already moved to the next day, `last_fired` set. The stamp
    is written as an aware time and read back through the store's naive-UTC round trip (`_ensure_utc`,
    `firestore_agent_note_adapter.py:323`); a stamp written as a local naive time would not compare equal either.

    Regression: the stamp stored as a string, or not stored (`mark_fire_delivered`, `firestore_agent_note_adapter.py:304-320`):
    the second delivery then posts again. (The mock tests `tests/unit/handlers/test_worker_handler.py:1077-1093,1228-1243`
    hand the handler a note that is already stamped; none runs the stamp through the store.)"""
    world = RedeliveryWorld()
    await world.set_primary_channel()
    world.router.always(ok(TEXT))
    note_id = await world.seed_note(monkeypatch, due=T0 + timedelta(days=1), recurrence="FREQ=DAILY",
                                    last_fired=T0 + timedelta(seconds=30), created_at=T0 - timedelta(days=1))
    payload = world.payload("execute_reminder", note_id=note_id, user_id=USER, due_at=T0.isoformat())

    twice = await deliver_twice(world.handler, payload, gathered=False)

    assert twice.answers == [({"status": "ok"}, 200), ({"status": "already_delivered"}, 200)]
    assert world.texts == [TEXT]
    assert len(world.router.messages) == 1
    stored = await world.notes.get_note(USER, note_id)
    assert stored is not None, "a recurrent note is kept for its next fire"
    assert stored.last_delivered_due == T0
