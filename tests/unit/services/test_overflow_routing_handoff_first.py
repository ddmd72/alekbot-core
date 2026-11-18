"""Owner decision O2 (P11-G2-3): an overflow batch is queued under a deterministic id before the session store cuts it,
and `route_overflow` raises when the batch or its task could not be queued, so the store keeps the messages and hands
them off again. The real `OverflowRoutingService` over the real `FirestoreConsolidationQueue` /
`FirestoreCompanionExtractionQueue` on one `EvalDb` and the real `GcpTaskQueue` over a recording Cloud Tasks client;
the session store's side is in `tests/unit/adapters/test_session_store_kit.py`.
"""
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import ServiceUnavailable

from src.adapters.firestore_companion_extraction_queue import FirestoreCompanionExtractionQueue
from src.adapters.firestore_consolidation_queue import FirestoreConsolidationQueue
from src.domain.channel_binding import ChannelBinding
from src.domain.companion_config import CompanionConfig
from src.domain.consolidation import handoff_key
from src.domain.llm import Message, MessagePart
from src.services.overflow_routing_service import OverflowRoutingService
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

QUEUE, CQUEUE = "eval_consolidation_queue", "eval_companion_queue"
SID = "u1:C1"


class Bindings:
    def __init__(self, binding=None) -> None:
        self.binding = binding

    async def get(self, channel_id):
        return self.binding


class Users:
    async def get_user(self, user_id):
        return SimpleNamespace(account_id="a1")


def world(binding=None):
    db = EvalDb()
    env = SimpleNamespace(consolidation_queue_collection=QUEUE, companion_extraction_queue_collection=CQUEUE)
    tasks, client = make_task_queue()
    routing = OverflowRoutingService(
        channel_binding_service=Bindings(binding), user_repo=Users(),
        consolidation_queue=FirestoreConsolidationQueue(db, env),
        companion_extraction_queue=FirestoreCompanionExtractionQueue(db, env), task_queue=tasks)
    return SimpleNamespace(db=db, routing=routing, client=client)


def batch():
    return [Message(role="user", parts=[MessagePart(text="When is the car service?")], created_at=1000.0),
            Message(role="model", parts=[MessagePart(text="On 2026-11-05.")], created_at=1001.0)]


async def test_a_repeated_hand_off_of_the_same_messages_is_one_batch_under_its_handoff_key():
    w = world()
    await w.routing.route_overflow("u1", SID, batch())
    await w.routing.route_overflow("u1", SID, batch())
    assert list(w.db.collection(QUEUE).docs) == [f"batch_{handoff_key(SID, batch())}"]
    assert [t for t, _ in w.client.worker_tasks()] == ["consolidation", "consolidation"]


async def test_a_failed_task_enqueue_raises_after_the_batch_was_stored(monkeypatch):
    w = world()

    def down(*args, **kwargs):
        raise ServiceUnavailable("503 UNAVAILABLE cloud tasks")

    monkeypatch.setattr(w.client, "create_task", down)
    with pytest.raises(ServiceUnavailable):
        await w.routing.route_overflow("u1", SID, batch())
    assert list(w.db.collection(QUEUE).docs) == [f"batch_{handoff_key(SID, batch())}"]


async def test_a_companion_batch_is_stored_under_its_handoff_key_too():
    binding = ChannelBinding(channel_id="C1", agent_type="tutor", intent="tutor_chat", created_by="u1",
                             companion_config=CompanionConfig(window_threshold=50, batch_size=30))
    w = world(binding)
    await w.routing.route_overflow("u1", SID, batch())
    await w.routing.route_overflow("u1", SID, batch())
    assert list(w.db.collection(CQUEUE).docs) == [f"cbatch_{handoff_key(SID, batch())}"]
    assert w.db.collection(QUEUE).docs == {}
