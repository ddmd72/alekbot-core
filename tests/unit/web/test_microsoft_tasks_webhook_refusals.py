"""Microsoft Graph change notifications that must change nothing (P7, inventory §4 and §5;
`src/web/microsoft_tasks_webhook.py:66-162`).

The real TaskSetupService over the real task-config repository on the evaluating Firestore fake; the queue records what
was enqueued, the indexing service what was indexed. Graph itself is never reached (Tripwire).
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from quart import Quart

from src.adapters.firestore_task_config_repository import FirestoreTaskConfigRepository
from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from src.services.task_setup_service import TaskSetupService
from src.web.microsoft_tasks_webhook import create_microsoft_tasks_webhook_blueprint
from tests.support.firestore_eval import EvalDb
from tests.unit.web.gate_fakes import RecordingTaskQueue, Tripwire
from src.composition.connector_registry import ConnectorRegistry

SECRET = "p7-graph-client-state"


class RecordingTaskIndexing:
    def __init__(self) -> None:
        self.calls: list = []

    async def index_task_by_ref(self, user_id, list_id, task_id):
        self.calls.append(("index", user_id, list_id, task_id))

    async def deindex_task(self, user_id, task_id):
        self.calls.append(("deindex", user_id, task_id))


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.touched: list = []
        self.queue = RecordingTaskQueue()
        self.indexing = RecordingTaskIndexing()
        self.config = FirestoreTaskConfigRepository(self.db, SimpleNamespace(task_config_collection="task_config"))
        setup = TaskSetupService(
            lifecycle=Tripwire("graph_lifecycle", self.touched), task_config=self.config,
            tasks_provider=Tripwire("tasks_provider", self.touched),
            oauth_credentials=Tripwire("oauth_credentials", self.touched),
            task_search_index=Tripwire("task_search_index", self.touched), task_queue=self.queue,
            notification_url_base="https://app.example/webhook/microsoft-tasks", connector=ConnectorRegistry().get("microsoft_todo"),
        )
        app = Quart("graph_webhook")
        app.register_blueprint(create_microsoft_tasks_webhook_blueprint(
            task_indexing=self.indexing, task_setup=setup, webhook_secret=SECRET))
        self.client = app.test_client()

    async def seed_user_a(self) -> None:
        # user-a owns subscription sub-A on list LA, fresh for 3 more days (no renewal due).
        await self.config.save_config("user-a", TaskUserConfig(primary_list_id="LA", subscriptions=[
            TaskSubscriptionConfig(sub_id="sub-A", list_id="LA",
                                   expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=3))]))

    def touched_user(self, user_id: str) -> list:
        return ([t for t in self.queue.tasks if (t.get("payload") or {}).get("user_id") == user_id]
                + [c for c in self.indexing.calls if c[1] == user_id])


def _notification(resource="me/todo/lists/LA/tasks", client_state=SECRET, sub_id="sub-A"):
    return {"value": [{"subscriptionId": sub_id, "changeType": "updated", "clientState": client_state,
                       "resource": resource}]}


async def test_valid_notification_reindexes_the_subscribers_list():
    # Positive control: the refusals below are not a route that ignores everything.
    w = World()
    await w.seed_user_a()
    resp = await w.client.post("/webhook/microsoft-tasks/user-a", json=_notification())
    assert resp.status_code == 202
    assert w.queue.tasks == [{"task_type": "reindex_task_list", "payload": {"user_id": "user-a", "list_id": "LA"}}]


async def test_invalid_json_is_400_and_nothing_is_indexed():
    w = World()
    await w.seed_user_a()
    resp = await w.client.post("/webhook/microsoft-tasks/user-a", data=b"{not json",
                               headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    assert w.queue.tasks == [] and w.indexing.calls == [] and w.touched == []


@pytest.mark.parametrize("resource", ["me/todo/tasks", "", "subscriptions/sub-A"])
async def test_unparsable_resource_is_skipped_and_nothing_is_indexed(resource):
    w = World()
    await w.seed_user_a()
    resp = await w.client.post("/webhook/microsoft-tasks/user-a", json=_notification(resource=resource))
    assert resp.status_code == 202   # Graph requires 202 for every change notification
    assert w.queue.tasks == [] and w.indexing.calls == [] and w.touched == []


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="known defect: clientState is one shared secret and the path <user_id> is not tied to the "
                          "subscription, so a notification for user-a's sub posted to another user's URL is applied "
                          "to that user (src/web/microsoft_tasks_webhook.py:66-142)")
async def test_a_notification_sent_to_another_users_url_touches_nothing_of_theirs():
    w = World()
    await w.seed_user_a()
    resp = await w.client.post("/webhook/microsoft-tasks/user-b", json=_notification())
    assert resp.status_code == 202
    assert w.touched_user("user-b") == []
