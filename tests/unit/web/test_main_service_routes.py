"""The main service's `/health`, `/favicon.ico` and `/worker` routes, on a bare Quart app with the blueprint.

The gate is a plain async callable (the real one is covered by `test_main_worker_route_gate.py` and the OIDC tests);
the handler is a fake that records what reached it and the memory-telemetry scope it ran in (read through the real
`memory_scope` hooks)."""
import os
from typing import Any, Dict, List, Optional, Tuple

import pytest
from quart import Quart

from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.domain.memory_telemetry import SCOPE_WORKER_TASK, ScopeRecord
from src.utils import memory_scope as ms
from src.web.main_service_routes import create_main_service_blueprint

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "src", "web", "static")


class _Hooks:
    def __init__(self) -> None:
        self.records: List[ScopeRecord] = []

    def usage_bytes(self) -> Optional[int]:
        return None

    def on_scope_end(self, record: ScopeRecord) -> None:
        self.records.append(record)


class _FakeWorker:
    def __init__(self, answer: Tuple[Dict[str, Any], int] = ({"status": "ok"}, 200)) -> None:
        self.answer = answer
        self.calls: List[Tuple[dict, Optional[CloudTaskDelivery], Dict[str, str]]] = []
        self.open_scopes: List[Tuple[str, str]] = []

    async def handle(self, payload, delivery=None, headers=None):
        self.calls.append((payload, delivery, headers))
        self.open_scopes = [(s.kind, s.name) for s in ms.active_scopes()]
        return self.answer


class _Gate:
    def __init__(self, allow: bool) -> None:
        self.allow = allow
        self.seen: List[Optional[str]] = []

    async def __call__(self, authorization: Optional[str]) -> bool:
        self.seen.append(authorization)
        return self.allow


@pytest.fixture
def hooks():
    h = _Hooks()
    ms.install_hooks(h)
    yield h
    ms.uninstall_hooks()


def _client(worker: _FakeWorker, gate: _Gate):
    app = Quart(__name__)
    app.register_blueprint(create_main_service_blueprint(worker, gate, static_dir=STATIC_DIR))
    return app.test_client()


async def test_a_refused_call_is_401_and_never_reaches_the_handler(hooks):
    worker, gate = _FakeWorker(), _Gate(False)
    response = await _client(worker, gate).post("/worker", json={"task_type": "daily_email_review"},
                                                headers={"Authorization": "Bearer t"})
    assert response.status_code == 401
    assert await response.get_json() == {"error": "unauthorized"}
    assert gate.seen == ["Bearer t"]
    assert worker.calls == [] and hooks.records == []


async def test_an_allowed_call_hands_payload_headers_and_delivery_to_the_handler():
    worker = _FakeWorker()
    payload = {"task_type": "daily_email_review", "user_id": "usr-1", "account_id": "acc-1"}
    response = await _client(worker, _Gate(True)).post("/worker", json=payload, headers={
        "X-CloudTasks-TaskName": "task-9", "X-CloudTasks-TaskRetryCount": "2", "X-CloudTasks-TaskExecutionCount": "3",
    })
    assert response.status_code == 200
    [(got_payload, delivery, headers)] = worker.calls
    assert got_payload == payload
    assert delivery == CloudTaskDelivery(task_name="task-9", retry_count=2, execution_count=3)
    assert {k.lower(): v for k, v in headers.items()}["x-cloudtasks-taskname"] == "task-9"


async def test_a_call_without_cloud_tasks_headers_has_no_delivery():
    worker = _FakeWorker()
    await _client(worker, _Gate(True)).post("/worker", json={"task_type": "daily_email_review"})
    [(_, delivery, _)] = worker.calls
    assert delivery is None


@pytest.mark.parametrize("kwargs", [{}, {"data": "not json", "headers": {"Content-Type": "application/json"}}])
async def test_no_or_invalid_json_reaches_the_handler_as_an_empty_payload(kwargs):
    worker = _FakeWorker()
    response = await _client(worker, _Gate(True)).post("/worker", **kwargs)
    assert response.status_code == 200
    assert worker.calls[0][0] == {}


async def test_the_handlers_body_and_status_are_returned_as_is():
    worker = _FakeWorker(({"error": "boom"}, 500))
    response = await _client(worker, _Gate(True)).post("/worker", json={"task_type": "daily_email_review"})
    assert response.status_code == 500
    assert await response.get_json() == {"error": "boom"}


async def test_the_handler_runs_in_a_memory_scope_named_after_the_task_type(hooks):
    worker = _FakeWorker()
    await _client(worker, _Gate(True)).post("/worker", json={"task_type": "daily_email_review"})
    assert worker.open_scopes == [(SCOPE_WORKER_TASK, "daily_email_review")]
    assert [(r.kind, r.name) for r in hooks.records] == [(SCOPE_WORKER_TASK, "daily_email_review")]


@pytest.mark.parametrize("payload", [{"user_id": "usr-1"}, {"task_type": ""}, {"task_type": None}])
async def test_a_payload_without_a_task_type_runs_in_the_slack_event_scope(hooks, payload):
    worker = _FakeWorker()
    await _client(worker, _Gate(True)).post("/worker", json=payload)
    assert worker.open_scopes == [(SCOPE_WORKER_TASK, "slack_event")]


async def test_health_answers_healthy_in_http_mode():
    response = await _client(_FakeWorker(), _Gate(False)).get("/health")
    assert response.status_code == 200
    assert await response.get_json() == {"status": "healthy", "mode": "http"}


async def test_favicon_is_the_logo_png():
    response = await _client(_FakeWorker(), _Gate(False)).get("/favicon.ico")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert (await response.get_data()).startswith(b"\x89PNG")
