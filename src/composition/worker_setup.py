"""Composition helper: the `/worker` task runners that live outside `WorkerHandler` (composition-root assessment §3 B).

`build_application_graph` passes the returned registry to `WorkerHandler(tasks=...)`, which merges it over the tasks it
still serves itself and refuses a key it already has. Each runner owns only its own dependencies; a feature whose
dependency is absent gets a `DisabledTask` answering the refusal the task gave before it left the handler.
"""
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional

from ..domain.worker_task import WorkerTask
from ..ports.agent_note_port import AgentNotePort
from ..ports.alert_sink import AlertSinkPort
from ..ports.user_repository import UserRepository
from ..services.disabled_task import DisabledTask
from ..services.execute_reminder_task import ExecuteReminderTask
from ..services.user_notification_service import UserNotificationService


def build_worker_tasks(*, notes_port: Optional[AgentNotePort], user_repo: UserRepository,
                       ensure_agents: Callable[[str], Awaitable[Any]],
                       notification_service: UserNotificationService,
                       alert_sink: Optional[AlertSinkPort]) -> Mapping[str, WorkerTask]:
    """Task type -> runner for every task served outside `WorkerHandler`."""
    tasks: Dict[str, WorkerTask] = {}
    if notes_port is None:
        tasks["execute_reminder"] = DisabledTask(
            "execute_reminder: notes_port not configured", 501, {"error": "notes_port not configured"})
    else:
        tasks["execute_reminder"] = ExecuteReminderTask(
            notes_port=notes_port, user_repo=user_repo, ensure_agents=ensure_agents,
            notification_service=notification_service, alert_sink=alert_sink)
    return tasks
