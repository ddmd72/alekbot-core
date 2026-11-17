"""WorkerHandler merges the runners composition passes (`tasks`) over its own and refuses a task type twice (S5b)."""
from unittest.mock import MagicMock

import pytest

from src.handlers.worker_handler import WorkerHandler


class _Runner:
    def __init__(self) -> None:
        self.calls = []

    async def __call__(self, payload, headers):
        self.calls.append((payload, headers))
        return {"ran": "runner"}, 202


def _handler(**kwargs) -> WorkerHandler:
    return WorkerHandler(
        agent_worker_handler=MagicMock(), email_indexing_service=MagicMock(),
        consolidation_service=MagicMock(), agent_factory=MagicMock(), user_repo=MagicMock(), **kwargs,
    )


async def test_a_passed_runner_is_dispatched_with_the_payload_and_headers():
    runner = _Runner()
    h = _handler(tasks={"execute_reminder": runner})
    payload = {"task_type": "execute_reminder", "note_id": "n1"}

    assert await h.handle(payload, headers={"x-trace-id": "t"}) == ({"ran": "runner"}, 202)
    assert runner.calls == [(payload, {"x-trace-id": "t"})]
    assert "execute_reminder" in h.task_types()
    assert _handler().task_types() == h.task_types() - {"execute_reminder"}


async def test_without_the_runner_the_task_type_is_unknown():
    assert await _handler().handle({"task_type": "execute_reminder"}) == ({"error": "unknown task_type"}, 400)


@pytest.mark.parametrize("key", ["email_indexing", "<queued slack event>"])
def test_a_task_type_the_handler_serves_itself_is_refused(key):
    with pytest.raises(ValueError, match=f"already serves: \\['{key}'\\]"):
        _handler(tasks={key: _Runner(), "execute_reminder": _Runner()})
