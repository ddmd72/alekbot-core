"""Graph notifications from work accounts (a task id in the resource) and the failure paths of one notification
(P7, C1: `src/web/microsoft_tasks_webhook.py` held at 100 % lines).

The real TaskSetupService over the real task-config repository on the evaluating Firestore fake; Graph (the
lifecycle port) is scripted, the index records what was indexed."""
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


class Index:
    def __init__(self, fails: bool = False) -> None:
        self.fails = fails
        self.calls: list = []

    async def index_task_by_ref(self, user_id, list_id, task_id):
        if self.fails:
            raise RuntimeError("graph 503 while fetching the task")
        self.calls.append(("index", user_id, list_id, task_id))

    async def deindex_task(self, user_id, task_id):
        self.calls.append(("deindex", user_id, task_id))


class GraphRenewalDown:
    """The lifecycle port with Graph's renew call failing."""

    def __init__(self) -> None:
        self.renewals = 0

    async def renew_subscription(self, user_id, sub_id):
        self.renewals += 1
        raise RuntimeError("graph 503 on PATCH /subscriptions")


class World:
    def __init__(self, index: Index = None, lifecycle=None) -> None:
        self.db = EvalDb()
        self.touched: list = []
        self.queue = RecordingTaskQueue()
        self.index = index or Index()
        self.lifecycle = lifecycle or Tripwire("graph_lifecycle", self.touched)
        self.config = FirestoreTaskConfigRepository(self.db, SimpleNamespace(task_config_collection="task_config"))
        setup = TaskSetupService(
            lifecycle=self.lifecycle, task_config=self.config, tasks_provider=Tripwire("tasks_provider", self.touched),
            oauth_credentials=Tripwire("oauth_credentials", self.touched),
            task_search_index=Tripwire("task_search_index", self.touched), task_queue=self.queue,
            notification_url_base="https://app.example/webhook/microsoft-tasks", connector=ConnectorRegistry().get("microsoft_todo"))
        app = Quart("graph_webhook_c1")
        app.register_blueprint(create_microsoft_tasks_webhook_blueprint(
            task_indexing=self.index, task_setup=setup, webhook_secret=SECRET))
        self.client = app.test_client()

    async def subscribe(self, expires_in: timedelta) -> None:
        await self.config.save_config("user-a", TaskUserConfig(primary_list_id="LA", subscriptions=[
            TaskSubscriptionConfig(sub_id="sub-A", list_id="LA",
                                   expires_at=datetime.now(timezone.utc).replace(tzinfo=None) + expires_in)]))

    async def notify(self, change_type: str, resource: str = "me/todo/lists/LA/tasks/T1"):
        return await self.client.post("/webhook/microsoft-tasks/user-a", json={"value": [{
            "subscriptionId": "sub-A", "changeType": change_type, "clientState": SECRET, "resource": resource}]})


@pytest.mark.parametrize("change_type,expected", [
    ("created", [("index", "user-a", "LA", "T1")]),
    ("updated", [("index", "user-a", "LA", "T1")]),
    ("deleted", [("deindex", "user-a", "T1")]),
    ("moved", []),                                   # any other change type is ignored
])
async def test_a_task_level_notification_updates_exactly_that_task(change_type, expected):
    w = World()
    await w.subscribe(timedelta(days=3))
    resp = await w.notify(change_type)
    assert resp.status_code == 202
    assert w.index.calls == expected
    assert w.queue.tasks == [] and w.touched == []    # no list reindex, Graph untouched (renewal not due)


async def test_a_task_that_cannot_be_indexed_is_still_acknowledged():
    w = World(index=Index(fails=True))
    await w.subscribe(timedelta(days=3))
    resp = await w.notify("updated")
    assert resp.status_code == 202                    # Graph must get its 202, or it disables the subscription
    assert w.index.calls == [] and w.queue.tasks == []


async def test_a_failed_subscription_renewal_is_still_acknowledged_and_keeps_the_config():
    lifecycle = GraphRenewalDown()
    w = World(lifecycle=lifecycle)
    await w.subscribe(timedelta(hours=2))             # inside the 48 h renewal window
    before = dict(w.db.collection("task_config").docs["user-a"])
    resp = await w.notify("updated")
    assert resp.status_code == 202
    assert lifecycle.renewals == 1
    assert w.index.calls == [("index", "user-a", "LA", "T1")]
    assert w.db.collection("task_config").docs["user-a"] == before
