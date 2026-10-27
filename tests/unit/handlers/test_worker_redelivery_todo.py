"""The To Do family of `/worker` tasks, each delivered twice under two task names the claim does not merge (P15 card F).

Two Cloud Tasks deliveries with different task names (`t-1`, then `t-2`) both run; the claim only merges one name. The
production graph is built (`prod_graph`), Microsoft Graph is the shared aiohttp fake, and the assertions read what ends
up stored (the task-config document), what Graph received, and the bodies of the Cloud Tasks created.

Not tested here, on purpose:
- Gathered setup (two connects in one moment) is P15-D2, which the arbiter found overstated, so there is no xfail. Setup
  is enqueued once per OAuth callback, as an unnamed task (`src/web/oauth_app.py:802-807`), so two copies need two
  connects in the same instant.
- Gathered renewal of one user is the same class: the fan-out enqueues one renewal per user per daily run
  (`src/handlers/worker_handler.py:879-882`), so two copies need two sweeps, or a webhook renewal, racing. Neither guards
  its read -> register -> save (`src/services/task_setup_service.py:174-207,290-318`); that is a finding, not a test.
- `reindex_task_list` twice is idempotent by construction: the search-index document id and the short id are
  deterministic (`src/services/task_indexing_service.py:55-71`), pinned by
  `tests/unit/adapters/test_firestore_task_search_index.py:106`.
"""
from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from src.config.environment import EnvironmentConfig
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from tests.support import http_boundary_fake
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from tests.support.clock_instants import pin_datetime
from tests.support.oauth_credentials_store import creds
from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)
from tests.support.redelivery_world import T0, deliver_twice

pytestmark = pytest.mark.slow

GRAPH = "https://graph.microsoft.com/v1.0"
CONFIG_COLLECTION = EnvironmentConfig().task_config_collection


async def _connect(graph: Any) -> None:
    await graph.app.task_setup_service._oauth_credentials.save_credentials(
        creds("u1", "microsoft_todo", token_expiry=datetime.now(timezone.utc) + timedelta(hours=1)))


def _reindex_bodies() -> List[dict]:
    return [b for c in FakeCloudTasksClient.instances for b in c.created_bodies()
            if b.get("task_type") == "reindex_task_list"]


def _posts(fake: Any, url: str) -> list:
    return [r for r in fake.requests if r.method == "POST" and r.url == url]


async def test_setup_delivered_twice_creates_one_list_and_one_subscription(prod_graph, monkeypatch):
    """Regression: the subscription or the list id not persisted, so the second setup registers again. The adapter's
    per-instance list cache would hide only the list half; the stored document is what is asserted."""
    from src.adapters.microsoft import todo_adapter
    from src.services import task_setup_service
    pin_datetime(monkeypatch, todo_adapter, T0)
    pin_datetime(monkeypatch, task_setup_service, T0)
    fake = http_boundary_fake.install(monkeypatch)
    fake.todo_empty_account()
    await _connect(prod_graph)

    twice = await deliver_twice(prod_graph.app.worker_handler,
                                {"task_type": "setup_microsoft_todo", "user_id": "u1"}, gathered=False)

    assert not twice.raised
    assert twice.answers == [({"status": "ok"}, 200), ({"status": "ok"}, 200)]
    assert len(_posts(fake, f"{GRAPH}/me/todo/lists")) == 1
    assert len(_posts(fake, f"{GRAPH}/subscriptions")) == 1
    stored = prod_graph.offline.db.collection(CONFIG_COLLECTION).docs["u1"]
    assert stored["primary_list_id"] == "l1"
    assert [s["sub_id"] for s in stored["subscriptions"]] == ["s1"]
    assert _reindex_bodies() == [{"task_type": "reindex_task_list", "user_id": "u1", "list_id": "l1"}]
    assert fake.unrouted == []


async def test_a_renewal_delivered_twice_replaces_an_orphan_once(prod_graph, monkeypatch):
    """Regression: the replacement not saved, so the second run PATCHes `s0` again and registers another."""
    from src.adapters.microsoft import todo_adapter
    from src.services import task_setup_service
    pin_datetime(monkeypatch, todo_adapter, T0)
    pin_datetime(monkeypatch, task_setup_service, T0)
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("PATCH", f"{GRAPH}/subscriptions/s0", 404, {})
    fake.route("POST", f"{GRAPH}/subscriptions", 201, {"id": "s1"})
    await _connect(prod_graph)
    soon = (T0 + timedelta(hours=1)).replace(tzinfo=None)
    await prod_graph.app.task_setup_service._task_config.save_config(
        "u1", TaskUserConfig(primary_list_id="l1",
                             subscriptions=[TaskSubscriptionConfig(sub_id="s0", list_id="l1", expires_at=soon)]))

    twice = await deliver_twice(prod_graph.app.worker_handler,
                                {"task_type": "renew_task_subscriptions", "user_id": "u1"}, gathered=False)

    assert not twice.raised
    assert twice.answers == [({"status": "ok"}, 200), ({"status": "ok"}, 200)]
    assert len([r for r in fake.requests if r.method == "PATCH"]) == 1
    assert len(_posts(fake, f"{GRAPH}/subscriptions")) == 1
    stored = prod_graph.offline.db.collection(CONFIG_COLLECTION).docs["u1"]
    assert [s["sub_id"] for s in stored["subscriptions"]] == ["s1"]
    assert _reindex_bodies() == [{"task_type": "reindex_task_list", "user_id": "u1", "list_id": "l1"}]
    assert fake.unrouted == []
