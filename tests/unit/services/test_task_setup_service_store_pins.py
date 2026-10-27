"""Defects of `TaskSetupService` that the task config store's full-overwrite contract exposes (P11 · G3-3), pinned
through the real service over the real `FirestoreTaskConfigRepository` on the evaluating Firestore fake (H1).

The lifecycle (the provider boundary) and the task queue are two tiny in-test fakes; the service is built by `_service`
so a signature change of its constructor touches one line.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.task import TaskSubscriptionConfig
from src.services.task_setup_service import TaskSetupService
from tests.support.firestore_eval import EvalDb
from tests.support.redelivery_world import require

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
COL = "eval_task_config"


class _Lifecycle:
    """The provider boundary: the primary list is "L-primary"; a registered subscription expires in two days and is
    aware UTC, as the real adapter returns it."""

    async def ensure_primary_list(self, user_id: str) -> str:
        return "L-primary"

    async def register_subscription(self, user_id: str, list_id: str, notification_url_base: str) -> TaskSubscriptionConfig:
        return TaskSubscriptionConfig(sub_id="sub1", list_id=list_id,
                                      expires_at=datetime.now(timezone.utc) + timedelta(days=2))


class _Queue:
    def __init__(self) -> None:
        self.bodies: list = []

    async def enqueue_worker_task(self, task_type: str, payload: dict, *args, **kwargs):
        self.bodies.append((task_type, payload))


class World:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE)
        self.queue = _Queue()
        repo = FirestoreTaskConfigRepository(self.db, SimpleNamespace(task_config_collection=COL))
        self.service = _service(repo, self.queue)

    @property
    def docs(self) -> dict:
        return self.db.collection(COL).docs


def _service(repo, queue) -> TaskSetupService:
    return TaskSetupService(_Lifecycle(), repo, None, None, None, queue, "https://example.com")


@pytest.fixture
def world():
    return World()


async def test_setup_keeps_the_primary_list_id(world):
    """The passing control of P11-G3-3: `setup` stores the list id before it reads the config."""
    await world.service.setup("u1")
    doc = world.docs["u1"]
    assert doc["primary_list_id"] == "L-primary"
    assert [s["sub_id"] for s in doc["subscriptions"]] == ["sub1"]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P11-G3-3: ensure_subscriptions saves a stale config over the list id it just stored")
async def test_ensure_subscriptions_keeps_the_primary_list_id_it_stored(world):
    """With no stored config, `ensure_subscriptions` stores the list id and then saves the config it read before, whose
    `primary_list_id` is None, over it. Flipping fix (`task_setup_service.py`, after `set_primary_list_id_if_absent`):
    `config.primary_list_id = list_id` (re-reading the config there flips it too)."""
    await world.service.ensure_subscriptions("u1")
    doc = world.docs["u1"]
    require([s["sub_id"] for s in doc["subscriptions"]] == ["sub1"] and len(world.queue.bodies) == 1,
            "the subscription is registered and saved")
    assert doc["primary_list_id"] == "L-primary", f"stored primary_list_id: {doc['primary_list_id']!r}"
