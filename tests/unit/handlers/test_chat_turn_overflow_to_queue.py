"""Chat turns to a queued consolidation batch: the composed chain `tests/integration/test_sliding_window_e2e.py` ran on the
live database (retired under TEST_COVERAGE_SYSTEMIC_RFC P24), here on one `EvalDb`.

`ConversationHandler.handle_message` on the seam world (tests/support/seam_world.py), whose session store is replaced by
one wired as production wires it: `FirestoreSessionStore` with the overflow callback and threshold resolver of
`build_session_routing` (`session_routing_setup.py`, i.e. `OverflowRoutingService.route_overflow` over the real
`FirestoreConsolidationQueue` and the raw `GcpTaskQueue`), as `service_container.py` passes them. Only the window is
small (4 messages, batches of 2) so that three turns overflow once.

Each link has its own spec: the window (`tests/unit/adapters/test_session_store_kit.py`), the routing into the queue
(`tests/unit/services/test_overflow_routing_service_store_pins.py`), the queue's readers
(`tests/unit/adapters/test_consolidation_queue_store_kit.py`), the consolidation pass over a batch
(`tests/unit/agents/test_consolidation_tool_fields_e2e.py`). This file pins that a chat turn's own user message and reply
are what reach them: the oldest turn leaves the session and lands in the queue as one pending batch, and a consolidation
task is created.
"""
import asyncio

from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.composition.session_routing_setup import build_session_routing
from tests.support.seam_world import OTHER_SLACK, USER, ChatChannel, SeamFactory, SeamWorld, require, says, slack_turn

TURNS = [("When is the car service?", "On 2026-11-05 at the Springfield garage."),
         ("Remind me to call dad tomorrow.", "Done: call dad tomorrow at 10:00."),
         ("Book Clara's weekend in Coimbra.", "Booked: Coimbra, Saturday to Sunday.")]


def overflowing_world(monkeypatch) -> SeamWorld:
    world = SeamWorld(monkeypatch)
    routing = build_session_routing(world.db, world.env, user_repo=world.users, task_queue=world.queue)  # type: ignore[arg-type]
    world.sessions = FirestoreSessionStore(world.db, world.env.sessions_collection, max_history_length=4, batch_size=2,
                                           overflow_callback=routing.overflow_callback,
                                           threshold_resolver=routing.threshold_resolver)
    world.factory = SeamFactory(world.sessions, world.users)
    return world


async def test_the_oldest_chat_turn_overflows_into_one_pending_consolidation_batch(monkeypatch):
    world = overflowing_world(monkeypatch)
    world.agent("router_agent").play(*(says(reply) for _, reply in TURNS))
    handler = world.chat_handler()

    for text, _ in TURNS:
        await handler.handle_message(slack_turn(text), ChatChannel(*OTHER_SLACK))
    await asyncio.gather(*list(world.sessions._pending_tasks), return_exceptions=True)

    require(not world.unscripted(), "every agent call was scripted")
    history = world.history("u1:C2")
    assert [(role, text) for role, text, _ in history] == [
        ("user", TURNS[1][0]), ("model", TURNS[1][1]), ("user", TURNS[2][0]), ("model", TURNS[2][1])]

    queue = FirestoreConsolidationQueue(world.db, world.env)
    [batch] = await queue.get_pending_batches(user_id=USER, limit=10)
    assert (batch.user_id, batch.session_id) == (USER, "u1:C2")
    assert len(batch.messages) == 2
    first_user, first_reply = (repr(m) for m in batch.messages)
    assert TURNS[0][0] in first_user and TURNS[0][1] in first_reply
    assert not any(text in repr(batch.messages) for turn in TURNS[1:] for text in turn)
    assert [body["user_id"] for body in world.bodies("consolidation")] == [USER]
