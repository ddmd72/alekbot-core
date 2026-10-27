"""P11-G2-3, pinned through the real overflow chain: `FirestoreSessionStore` with the production callback
`OverflowRoutingService.route_overflow`, the real `FirestoreConsolidationQueue`, channel-binding and user repositories on
one `EvalDb`, and the real `GcpTaskQueue` over a recording Cloud Tasks client.

The memory map lists the loss as known (`docs/05_building_blocks/memory/README.md`, the row "enqueue of the batch fails
at overflow": lost, no recovery). The pin asserts a fix-shape-free outcome: after a failed enqueue, the cut messages are
still in SOME stored document (the session, the queue, or an outbox of any shape).
"""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_channel_binding_adapter import FirestoreChannelBindingAdapter
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.llm import Message, MessagePart
from src.services.channel_binding_service import ChannelBindingService
from src.services.overflow_routing_service import OverflowRoutingService
from tests.support.firestore_eval import EvalDb, EvalDocRef
from tests.support.recording_task_queue import make_task_queue
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
SESS, QUEUE = "eval_sessions", "eval_consolidation_queue"
SID = "u1:C1"
TEXTS = ["When is the car service?", "On 2026-11-05.", "Remind me to call dad.", "Done: call dad tomorrow.",
         "Book Clara's weekend in Coimbra."]


class Chain:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={SESS: "development_sessions",
                                                       QUEUE: "development_consolidation_queue"})
        env = SimpleNamespace(consolidation_queue_collection=QUEUE, domain_users_collection="eval_users",
                              firestore_collection_prefix="eval_", is_production=False)
        self.queue = FirestoreConsolidationQueue(self.db, env)
        self.tasks, self.client = make_task_queue()
        self.routing = OverflowRoutingService(
            channel_binding_service=ChannelBindingService(FirestoreChannelBindingAdapter(self.db, env)),
            user_repo=FirestoreUserRepository(self.db, env, None), consolidation_queue=self.queue,
            companion_extraction_queue=None, task_queue=self.tasks)
        self.store = FirestoreSessionStore(self.db, SESS, max_history_length=4, batch_size=2,
                                           overflow_callback=self.routing.route_overflow)

    async def five_appends(self) -> None:
        for text in TEXTS:
            await self.store.append_messages_batch(SID, [Message(role="user", parts=[MessagePart(text=text)])],
                                                   owner_id="u1")
        await asyncio.gather(*list(self.store._pending_tasks), return_exceptions=True)

    def stored_anywhere(self, text: str) -> bool:
        return any(text in repr(doc) for col in self.db.cols.values() for doc in col.docs.values())


class FailOnce:
    """The next plain `set` on the queue collection raises once, as a transient Firestore error does."""

    def __init__(self, monkeypatch) -> None:
        self.raised = 0
        original = EvalDocRef.set

        async def set_(ref, data, merge=False):
            if ref.col.name == QUEUE and self.raised == 0:
                self.raised += 1
                raise ServiceUnavailable("503 UNAVAILABLE the queue write failed")
            return await original(ref, data, merge)

        monkeypatch.setattr(EvalDocRef, "set", set_)


async def test_a_handed_off_batch_is_stored_in_the_queue_and_its_task_enqueued():
    chain = Chain()
    await chain.five_appends()
    batches = list(chain.db.collection(QUEUE).docs.values())
    assert [(b["user_id"], b["session_id"], b["status"]) for b in batches] == [("u1", SID, "pending")]
    assert TEXTS[0] in repr(batches[0]["messages"]) and TEXTS[1] in repr(batches[0]["messages"])
    assert [t for t, _ in chain.client.worker_tasks()] == ["consolidation"]
    assert TEXTS[0] not in repr(chain.db.collection(SESS).docs[SID]["history"])


@pytest.mark.xfail(strict=True, raises=AssertionError, reason=(
    "P11-G2-3: an overflow batch is cut from the session before its hand-off and lost when the enqueue fails "
    "(known loss, docs/05_building_blocks/memory/README.md:1259); fix is the owner's choice"))
async def test_a_cut_batch_survives_a_failed_enqueue(monkeypatch):
    """Flips when the cut batch is stored durably before (or with) the cut, or the hand-off is retried until stored."""
    chain = Chain()
    fault = FailOnce(monkeypatch)
    await chain.five_appends()
    require(fault.raised == 1, "the overflow hand-off ran and its enqueue failed once")
    require(TEXTS[4] in repr(chain.db.collection(SESS).docs[SID]["history"]), "the triggering append was stored")
    assert chain.stored_anywhere(TEXTS[0])
