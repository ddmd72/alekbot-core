"""P12 on the paused Cloud Tasks test queue: the real `GcpTaskQueue` enqueues, Cloud Tasks stores the task, and nothing is
dispatched (TEST_COVERAGE_SYSTEMIC_RFC §4, "Cloud Tasks: a paused test queue, so enqueue is real and nothing is
dispatched"). Run by hand (`make test-cloud`), never in `make check` / `make ci`.

Reuses the `cloud` fixture (its skips and the test service account's credentials). The queue name comes from
`TEST_TASKS_QUEUE` (the location from the optional `TEST_TASKS_LOCATION`, else the deploy region) and is never printed.
Without `TEST_TASKS_QUEUE` every test skips.

Guards, before any task is created: the queue name ends in `-test` and is none of the live queues the app config produces
(`agent-tasks-*`, `alek-bot-tasks-*`, `drive-sync-*` for both suffixes); the queue is PAUSED (a queue that is not is refused,
never paused here); the client uses the test service account's credentials. The adapter is built from the deploy topology's
literals directly, never through composition or `load_settings()` (§4 guard 5: under `APP_ENV=test` the queue names switch
suffix). The task target is a reserved `.invalid` host and carries no OIDC identity, so a dispatch could not reach the service.
Every task the run creates is deleted in the end; names are unique per run (Cloud Tasks blocks a deleted name for about an hour).
"""
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from google.cloud import tasks_v2

from src.adapters import gcp_task_queue
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.composition.deploy_topology import _REGION, _SLACK_INTAKE_REGION
from src.composition.drive_indexing_setup import drive_queue_name
from src.composition.deploy_topology import DeployTopology
from tests.cloud.conftest import _setting

RUN_ID = uuid.uuid4().hex[:10]
TARGET = "https://paused-test-queue.invalid"


def _live_queue_names() -> set:
    names = set()
    for suffix in ("dev", "prod"):
        topology = DeployTopology(service_url=TARGET, queue_suffix=suffix, region=_REGION, slack_intake_region=_SLACK_INTAKE_REGION)
        names |= {f"agent-tasks-{suffix}", f"alek-bot-tasks-{suffix}", drive_queue_name(topology)}
    return names


@pytest.fixture(scope="module")
def paused_queue(cloud):
    queue = _setting("TEST_TASKS_QUEUE")
    if not queue:
        pytest.skip("paused queue not configured: TEST_TASKS_QUEUE must be set")
    if not queue.endswith("-test"):
        pytest.fail("guard: the test queue name does not end in -test")
    if queue in _live_queue_names():
        pytest.fail("guard: the test queue is a live queue")
    location = _setting("TEST_TASKS_LOCATION") or _REGION
    client = tasks_v2.CloudTasksClient(credentials=cloud.credentials)
    if getattr(client._transport._credentials, "service_account_email", None) != _setting("TEST_SA_EMAIL"):
        pytest.fail("guard: the constructed Cloud Tasks client does not use the test service account")
    with patch.object(gcp_task_queue.tasks_v2, "CloudTasksClient", lambda: client):
        adapter = GcpTaskQueue(project_id=cloud.project, location=location, queue_name=queue, service_url=TARGET)
    if adapter.client is not client:
        pytest.fail("guard: the adapter does not use the guarded client")
    if client.get_queue(name=adapter.queue_path).state != tasks_v2.Queue.State.PAUSED:
        pytest.fail("guard: the test queue is not PAUSED (a queue that dispatches is refused, never paused by the test)")
    created = []
    yield SimpleNamespace(adapter=adapter, client=client, created=created)
    for name in created:
        try:
            client.delete_task(name=name)
        except Exception:       # already gone: the cleanup must not hide the test's own verdict
            pass


def _key(label: str) -> str:
    return f"parity-{RUN_ID}-{label}"


def _assert_queued_not_dispatched(queue, name: str) -> None:
    queue.created.append(name)
    task = queue.client.get_task(request={"name": name, "response_view": tasks_v2.Task.View.FULL})
    assert task.name == name
    assert task.dispatch_count == 0 and task.response_count == 0
    assert not task.first_attempt.dispatch_time and not task.last_attempt.dispatch_time
    assert task.http_request.url == f"{TARGET}/worker"
    assert queue.client.get_queue(name=queue.adapter.queue_path).state == tasks_v2.Queue.State.PAUSED
    assert name in {t.name for t in queue.client.list_tasks(parent=queue.adapter.queue_path)}


async def test_a_worker_task_is_stored_on_the_paused_queue_and_not_dispatched(paused_queue):
    name = await paused_queue.adapter.enqueue_worker_task("parity_probe", {"run": RUN_ID}, dedup_key=_key("worker"))
    assert name and name.startswith(paused_queue.adapter.queue_path + "/tasks/parity_probe-")
    _assert_queued_not_dispatched(paused_queue, name)


async def test_an_agent_task_is_stored_on_the_paused_queue_and_not_dispatched(paused_queue):
    name = await paused_queue.adapter.enqueue_agent_task("probe_agent", "probe", "q", {"run": RUN_ID}, dedup_key=_key("agent"))
    assert name.startswith(paused_queue.adapter.queue_path + "/tasks/agent_execution-")
    _assert_queued_not_dispatched(paused_queue, name)


async def test_a_second_worker_task_with_the_same_dedup_key_is_refused_as_already_exists_and_returns_none(paused_queue):
    """`decisions/worker_task_dedup.md`: Cloud Tasks refuses a second task of the same name while the first is queued
    (ALREADY_EXISTS); `enqueue_worker_task` turns that into `None`, never a raise."""
    key = _key("dedup-worker")
    first = await paused_queue.adapter.enqueue_worker_task("parity_probe", {"run": RUN_ID}, dedup_key=key)
    assert first
    paused_queue.created.append(first)
    assert await paused_queue.adapter.enqueue_worker_task("parity_probe", {"run": RUN_ID, "again": True}, dedup_key=key) is None
    queued = [t for t in paused_queue.client.list_tasks(parent=paused_queue.adapter.queue_path) if t.name == first]
    assert len(queued) == 1 and queued[0].dispatch_count == 0


async def test_a_second_agent_task_with_the_same_dedup_key_returns_the_name_of_the_first(paused_queue):
    """`enqueue_agent_task` treats ALREADY_EXISTS as "the task exists": it returns the name, the same one."""
    key = _key("dedup-agent")
    first = await paused_queue.adapter.enqueue_agent_task("probe_agent", "probe", "q", {"run": RUN_ID}, dedup_key=key)
    paused_queue.created.append(first)
    second = await paused_queue.adapter.enqueue_agent_task("probe_agent", "probe", "q", {"run": RUN_ID}, dedup_key=key)
    assert second == first
    assert len([t for t in paused_queue.client.list_tasks(parent=paused_queue.adapter.queue_path) if t.name == first]) == 1
