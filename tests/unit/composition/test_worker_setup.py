"""build_worker_tasks: the runner registry `WorkerHandler` receives from composition (S5b)."""
import logging

from src.services.disabled_task import DisabledTask
from src.services.execute_reminder_task import ExecuteReminderTask
from src.composition.worker_setup import build_worker_tasks


class _Factory:
    async def ensure_agents_for_user(self, user_id: str) -> None:
        return None


def _deps(notes_port):
    factory = _Factory()
    return dict(notes_port=notes_port, user_repo=object(), ensure_agents=factory.ensure_agents_for_user,
                notification_service=object(), alert_sink=object(), task_dispatch=None, video_registry=None,
                quota_service=None, media_storage=None, link_service=None, short_link_service=None,
                deep_research_fetch=None, prompt_content_store=None)


def test_with_a_note_store_the_reminder_runner_holds_exactly_the_given_dependencies():
    deps = _deps(object())

    tasks = build_worker_tasks(**deps)

    assert set(tasks) == {"execute_reminder"}
    runner = tasks["execute_reminder"]
    assert type(runner) is ExecuteReminderTask
    assert runner._notes_port is deps["notes_port"]
    assert runner._user_repo is deps["user_repo"]
    assert runner._ensure_agents == deps["ensure_agents"]
    assert runner._notification is deps["notification_service"]
    assert runner._alert_sink is deps["alert_sink"]


async def test_without_a_note_store_the_task_answers_the_refusal_it_gave_inside_the_handler(caplog):
    tasks = build_worker_tasks(**_deps(None))

    assert set(tasks) == {"execute_reminder"}
    assert type(tasks["execute_reminder"]) is DisabledTask
    payload = {"task_type": "execute_reminder", "note_id": "n1", "user_id": "user-1", "due_at": "2026-05-01T06:05:00"}
    with caplog.at_level(logging.WARNING):
        assert await tasks["execute_reminder"](payload, {}) == ({"error": "notes_port not configured"}, 501)
    assert caplog.messages == ["[Worker] execute_reminder: notes_port not configured"]
