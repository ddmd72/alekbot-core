"""P11 card D: the real `GcpTaskQueue` behind its port, over a Cloud Tasks client with Cloud Tasks semantics.

`ValidatingTasksClient` is `FailingTasksClient` (a used name is refused, the next creates can fail before or after they
land) that also builds the SDK's `CreateTaskRequest` from every request first, so a misspelt field or a wrong type
fails here as the SDK would fail it offline. Name charset and deadline range are server-side rules the SDK does not
check; `test_names_and_deadlines_are_valid_for_cloud_tasks` checks them. Assertions read what the client recorded
(requests, decoded bodies), never calls. `schedule_time` is bounded by the real clock before and after the call: the
adapter imports the `datetime` module, which `pin_datetime` cannot pin (gap named for P16).
"""
import datetime as dt
import enum
import hashlib
import json
import re
from typing import Any, Dict, List
from unittest import mock

import pytest
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
from google.cloud import tasks_v2
from pydantic import BaseModel

from src.adapters.gcp_task_queue import GcpTaskQueue
from tests.support.recording_task_queue import SERVICE_URL
from tests.support.redelivery_world import FailingTasksClient, require

UTC = dt.timezone.utc
PARENT = "projects/p/locations/us-central1/queues/q"
SA = "worker-sa@example.com"
IDS = {"user_id": "u1", "account_id": "a1", "session_id": "u1:C1"}


class ValidatingTasksClient(FailingTasksClient):
    """Builds `tasks_v2.CreateTaskRequest` from each request before anything else (the SDK's own offline check)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.validated: List[Any] = []

    def create_task(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        self.validated.append(tasks_v2.CreateTaskRequest(parent=request["parent"], task=request["task"]))
        return super().create_task(request, **kwargs)


class _QueueRefusingClient(ValidatingTasksClient):
    def create_queue(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        raise ServiceUnavailable("503 create_queue")


def _build(sa=None, client_cls=ValidatingTasksClient):
    with mock.patch("google.cloud.tasks_v2.CloudTasksClient", client_cls):
        queue = GcpTaskQueue("p", "us-central1", "q", SERVICE_URL, service_account_email=sa)
    require(isinstance(queue.client, client_cls), "the queue was built over the kit's client")
    return queue, queue.client


@pytest.fixture
def qc():
    return _build()


def _tasks(client) -> List[Dict[str, Any]]:
    return [r["task"] for r in client.create_task_requests]


def _digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:32]


def _scheduled(task) -> dt.datetime:
    return task["schedule_time"].ToDatetime(tzinfo=UTC)


def _now() -> dt.datetime:
    return dt.datetime.now(UTC)


# ------------------------------------------------------------------------------------------- slack events

async def test_slack_event_request_and_body(qc):
    queue, client = qc
    before = _now()
    name = await queue.enqueue_slack_event({"type": "message", "text": "Rome hotel RM-55120"}, "u1:C1",
                                           trace_headers={"traceparent": "00-abc-def-01"}, busy_requeues=2,
                                           task_name="slack-e1")
    after = _now()
    [request] = client.create_task_requests
    task = request["task"]
    assert request["parent"] == PARENT
    assert task["http_request"]["url"] == f"{SERVICE_URL}/worker"
    assert task["http_request"]["http_method"] == tasks_v2.HttpMethod.POST
    assert task["http_request"]["headers"] == {"Content-Type": "application/json", "traceparent": "00-abc-def-01"}
    assert task["dispatch_deadline"].seconds == 1800
    assert "schedule_time" not in task and "oidc_token" not in task["http_request"]
    assert task["name"] == f"{PARENT}/tasks/slack-e1"
    assert isinstance(name, str) and name      # for logging only: no caller reads it, so its shape is not pinned
    [body] = client.created_bodies()
    assert (body["event"], body["session_id"], body["busy_requeues"]) == (
        {"type": "message", "text": "Rome hotel RM-55120"}, "u1:C1", 2)
    enqueued_at = dt.datetime.fromisoformat(body["enqueued_at"])
    assert enqueued_at.tzinfo is not None and before <= enqueued_at <= after


async def test_slack_event_without_a_name_is_unnamed_and_returns_the_created_name(qc):
    queue, client = qc
    name = await queue.enqueue_slack_event({"type": "message"}, "u1:C1")
    [task] = _tasks(client)
    assert "name" not in task and isinstance(name, str) and name
    assert task["http_request"]["headers"] == {"Content-Type": "application/json"}


async def test_slack_event_delay_sets_schedule_time(qc):
    queue, client = qc
    before = _now()
    await queue.enqueue_slack_event({"type": "message"}, "u1:C1", delay_seconds=15, task_name="slack-e1-r1")
    after = _now()
    scheduled = _scheduled(_tasks(client)[0])
    assert before + dt.timedelta(seconds=15) - dt.timedelta(microseconds=1) <= scheduled <= after + dt.timedelta(seconds=15)


async def test_slack_event_same_name_twice_is_one_task(qc):
    queue, client = qc
    first = await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-e1")
    second = await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-e1")
    assert all(isinstance(n, str) and n for n in (first, second))
    assert len(client.create_task_requests) == 1


async def test_slack_event_lost_answer_then_same_name_is_one_task(qc):
    queue, client = qc
    client.fail_next(landed=True)
    with pytest.raises(DeadlineExceeded):
        await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-e1")
    assert await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-e1")
    assert len(client.create_task_requests) == 1


async def test_slack_event_failure_not_landed_raises_and_queues_nothing(qc):
    queue, client = qc
    client.fail_next(landed=False)
    with pytest.raises(DeadlineExceeded):
        await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-e1")
    assert client.create_task_requests == [] and client.failed_attempts == 1


# -------------------------------------------------------------------------------------------- agent tasks

async def test_agent_task_body_and_optional_deadline(qc):
    queue, client = qc
    await queue.enqueue_agent_task("deep_research", "deep_research", "Lisbon PyCon trip", dict(IDS))
    await queue.enqueue_agent_task("deep_research", "deep_research", "Lisbon PyCon trip", dict(IDS),
                                   deadline_seconds=1800)
    first, second = _tasks(client)
    assert client.created_bodies()[0] == {"task_type": "agent_execution", "agent_id": "deep_research",
                                          "intent": "deep_research", "query": "Lisbon PyCon trip", "context": IDS}
    assert "name" not in first and "dispatch_deadline" not in first
    assert "name" not in second and second["dispatch_deadline"].seconds == 1800


async def test_agent_task_key_names_and_dedups(qc):
    queue, client = qc
    key = "deep_research_report:job-1"
    expected = f"{PARENT}/tasks/agent_execution-{_digest(key)}"
    first = await queue.enqueue_agent_task("html", "create_html_page", "q", dict(IDS), dedup_key=key)
    second = await queue.enqueue_agent_task("html", "create_html_page", "q", dict(IDS), dedup_key=key)
    assert first == second == expected
    assert [t["name"] for t in _tasks(client)] == [expected]


async def test_agent_task_lost_answer_then_same_key_is_one_task(qc):
    queue, client = qc
    key = "deep_research_report:job-1"
    client.fail_next(landed=True)
    with pytest.raises(DeadlineExceeded):
        await queue.enqueue_agent_task("html", "create_html_page", "q", dict(IDS), dedup_key=key)
    assert await queue.enqueue_agent_task("html", "create_html_page", "q", dict(IDS), dedup_key=key) == \
        f"{PARENT}/tasks/agent_execution-{_digest(key)}"
    assert len(client.create_task_requests) == 1


# ------------------------------------------------------------------------------------------- worker tasks

async def test_worker_task_body_and_namespaced_name(qc):
    queue, client = qc
    a = await queue.enqueue_worker_task("fact_review", {"account_id": "a1"}, dedup_key="a1:2026-10-27")
    b = await queue.enqueue_worker_task("fact_lifecycle_sweep", {"account_id": "a1"}, dedup_key="a1:2026-10-27")
    assert client.created_bodies() == [{"task_type": "fact_review", "account_id": "a1"},
                                       {"task_type": "fact_lifecycle_sweep", "account_id": "a1"}]
    assert [t["name"] for t in _tasks(client)] == [
        f"{PARENT}/tasks/fact_review-{_digest('a1:2026-10-27')}",
        f"{PARENT}/tasks/fact_lifecycle_sweep-{_digest('a1:2026-10-27')}"]
    assert (a, b) == tuple(t["name"] for t in _tasks(client))


async def test_worker_task_duplicate_returns_none(qc):
    queue, client = qc
    assert await queue.enqueue_worker_task("start_drive_sync", {"user_id": "u1"}, dedup_key="u1") is not None
    assert await queue.enqueue_worker_task("start_drive_sync", {"user_id": "u1"}, dedup_key="u1") is None
    assert len(client.create_task_requests) == 1


async def test_worker_task_deadline_and_delay(qc):
    queue, client = qc
    before = _now()
    await queue.enqueue_worker_task("execute_reminder", {"user_id": "u1", "note_id": "n1"}, delay_seconds=90,
                                    deadline_seconds=1500)
    after = _now()
    await queue.enqueue_worker_task("execute_reminder", {"user_id": "u1", "note_id": "n2"})
    timed, plain = _tasks(client)
    assert timed["dispatch_deadline"].seconds == 1500
    assert before + dt.timedelta(seconds=90) - dt.timedelta(microseconds=1) <= _scheduled(timed) <= \
        after + dt.timedelta(seconds=90)
    assert "dispatch_deadline" not in plain and "schedule_time" not in plain


async def test_worker_task_without_key_is_never_deduplicated(qc):
    queue, client = qc
    names = [await queue.enqueue_worker_task("reindex_task_list", {"user_id": "u1"}) for _ in range(2)]
    assert len(client.create_task_requests) == 2 and len(set(names)) == 2
    assert all("name" not in t for t in _tasks(client))


# ------------------------------------------------------------------------------------ the typed methods

async def test_consolidation_task(qc):
    queue, client = qc
    await queue.enqueue_consolidation_task("u1")
    await queue.enqueue_consolidation_task("u1")
    assert client.created_bodies() == [{"task_type": "consolidation", "user_id": "u1"}] * 2
    assert all(t["dispatch_deadline"].seconds == 1800 and "name" not in t for t in _tasks(client))


async def test_companion_task(qc):
    queue, client = qc
    await queue.enqueue_companion_consolidation_task("u1:C1")
    [task] = _tasks(client)
    assert client.created_bodies() == [{"task_type": "companion_consolidation", "session_id": "u1:C1"}]
    assert task["dispatch_deadline"].seconds == 600 and "name" not in task


async def test_email_indexing_task(qc):
    queue, client = qc
    await queue.enqueue_email_indexing_task("job-1")
    [task] = _tasks(client)
    assert client.created_bodies() == [{"task_type": "email_indexing", "job_id": "job-1"}]
    assert "dispatch_deadline" not in task and "name" not in task and "schedule_time" not in task


async def test_video_polling_task(qc):
    queue, client = qc
    before = _now()
    await queue.enqueue_video_generation_polling("req-1", "u1", "a1", session_id="u1:C1", duration_s=8, attempt=2)
    after = _now()
    await queue.enqueue_video_generation_polling("req-1", "u1", "a1", origin_platform="telegram", delay_seconds=0)
    delayed, now = _tasks(client)
    assert client.created_bodies() == [
        {"task_type": "video_generation_polling", "request_id": "req-1", "user_id": "u1", "account_id": "a1",
         "session_id": "u1:C1", "duration_s": 8, "origin_platform": None, "attempt": 2},
        {"task_type": "video_generation_polling", "request_id": "req-1", "user_id": "u1", "account_id": "a1",
         "session_id": "", "duration_s": 5, "origin_platform": "telegram", "attempt": 0}]
    assert before + dt.timedelta(seconds=30) - dt.timedelta(microseconds=1) <= _scheduled(delayed) <= \
        after + dt.timedelta(seconds=30)
    assert "schedule_time" not in now and "dispatch_deadline" not in delayed and "name" not in delayed


# ---------------------------------------------------------------------------------------- every method

def _every_method(queue):
    return {
        "slack": lambda: queue.enqueue_slack_event({"type": "message"}, "u1:C1"),
        "agent": lambda: queue.enqueue_agent_task("a", "i", "q", dict(IDS)),
        "consolidation": lambda: queue.enqueue_consolidation_task("u1"),
        "companion": lambda: queue.enqueue_companion_consolidation_task("u1:C1"),
        "video": lambda: queue.enqueue_video_generation_polling("req-1", "u1", "a1"),
        "email": lambda: queue.enqueue_email_indexing_task("job-1"),
        "worker": lambda: queue.enqueue_worker_task("reindex_task_list", {"user_id": "u1"}),
    }


METHODS = ["slack", "agent", "consolidation", "companion", "video", "email", "worker"]


@pytest.mark.parametrize("method", METHODS)
@pytest.mark.parametrize("sa, expected", [(SA, SA), (None, None), ("   ", None)], ids=["sa", "none", "blank"])
async def test_oidc_token_only_with_a_service_account(method, sa, expected):
    queue, client = _build(sa=sa)
    await _every_method(queue)[method]()
    [task] = _tasks(client)
    token = task["http_request"].get("oidc_token")
    assert (token["service_account_email"] if token else None) == expected
    assert client.validated[0].task.http_request.oidc_token.service_account_email == (expected or "")


@pytest.mark.parametrize("method", METHODS)
async def test_create_failure_propagates_for_every_method(qc, method):
    queue, client = qc
    client.fail_next(landed=False, exc=ServiceUnavailable("503"))
    with pytest.raises(ServiceUnavailable):
        await _every_method(queue)[method]()
    assert client.create_task_requests == []


# ---------------------------------------------------------------------------------------------- payloads

class Color(enum.Enum):
    RED = "red"


class Lang(str, enum.Enum):
    UK = "uk"


class Hit(BaseModel):
    seen_at: dt.datetime
    color: Color


async def test_payload_encoding(qc):
    queue, client = qc
    aware = dt.datetime(2026, 10, 18, 9, 30, tzinfo=UTC)
    naive = dt.datetime(2026, 10, 18, 9, 30)
    context = {**IDS, "hits": [Hit(seen_at=aware, color=Color.RED)], "day": dt.date(2026, 10, 18),
               "naive": naive, "lang": Lang.UK, "color": Color.RED, "nested": {"list": [aware, {"d": dt.date(2025, 8, 10)}]}}
    await queue.enqueue_agent_task("a", "i", "q", context)
    [body] = client.created_bodies()
    assert body["context"] == {**IDS, "hits": [{"seen_at": "2026-10-18T09:30:00+00:00", "color": "red"}],
                               "day": "2026-10-18", "naive": "2026-10-18T09:30:00", "lang": "uk", "color": "red",
                               "nested": {"list": ["2026-10-18T09:30:00+00:00", {"d": "2025-08-10"}]}}


async def test_unencodable_payload_raises_before_any_create(qc):
    queue, client = qc
    with pytest.raises(TypeError):
        await queue.enqueue_agent_task("a", "i", "q", {**IDS, "tags": {"rome"}})
    with pytest.raises(TypeError):
        await queue.enqueue_worker_task("reindex_task_list", {"user_id": "u1", "raw": {b"x"}})
    assert client.create_task_requests == [] and client.validated == []


async def test_names_and_deadlines_are_valid_for_cloud_tasks(qc):
    queue, client = qc
    await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-" + _digest("Ev1"))
    await queue.enqueue_slack_event({"type": "message"}, "u1:C1", task_name="slack-" + _digest("Ev1") + "-r12")
    await queue.enqueue_agent_task("a", "i", "q", dict(IDS), deadline_seconds=1800, dedup_key="deep_research_report:j:1")
    await queue.enqueue_worker_task("telegram_update", {"update": {}}, deadline_seconds=1800, dedup_key="tg:12345")
    await queue.enqueue_worker_task("fact_review", {}, deadline_seconds=900, dedup_key="fact-review:a1:2026-10-27")
    await queue.enqueue_consolidation_task("u1")
    await queue.enqueue_companion_consolidation_task("u1:C1")
    names = [t["name"].rsplit("/tasks/", 1)[1] for t in _tasks(client) if "name" in t]
    assert len(names) == 5 and all(re.fullmatch(r"[A-Za-z0-9_-]{1,500}", n) for n in names)
    deadlines = [t["dispatch_deadline"].seconds for t in _tasks(client) if "dispatch_deadline" in t]
    assert len(deadlines) == 7 and all(15 <= d <= 1800 for d in deadlines)
    assert len(client.validated) == 7


# ---------------------------------------------------------------------------------------------- the queue

async def test_create_queue_if_not_exists_creates_once(qc):
    queue, client = qc
    await queue.create_queue_if_not_exists()
    await queue.create_queue_if_not_exists()
    assert client.created_queues == [PARENT]


async def test_create_queue_failure_raises():
    queue, client = _build(client_cls=_QueueRefusingClient)
    with pytest.raises(ServiceUnavailable):
        await queue.create_queue_if_not_exists()
    assert client.created_queues == []


def test_bodies_are_json_bytes(qc):
    """The body the SDK carries is bytes of JSON (not a str): the request validates and decodes."""
    import asyncio
    queue, client = qc
    asyncio.run(queue.enqueue_email_indexing_task("job-1"))
    body = client.validated[0].task.http_request.body
    assert isinstance(body, bytes) and json.loads(body) == {"task_type": "email_indexing", "job_id": "job-1"}
