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
from ..ports.prompt_content_store import PromptContentStore
from ..services.deep_research_delivery import deliver_deep_research
from ..services.deep_research_failed_task import DeepResearchFailedTask
from ..services.deep_research_notice_sender import DeepResearchNoticeSender
from ..services.deep_research_webhook_event_task import DeepResearchWebhookEventTask
from ..services.disabled_task import DisabledTask
from ..services.execute_reminder_task import ExecuteReminderTask
from ..services.provider_registry import ProviderRegistry
from ..services.task_dispatch_service import TaskDispatchService
from ..services.user_notification_service import UserNotificationService
from ..services.video_generation_delivery import deliver_video
from ..services.video_generation_polling_task import VideoGenerationPollingTask


def build_worker_tasks(*, notes_port: Optional[AgentNotePort], user_repo: UserRepository,
                       ensure_agents: Callable[[str], Awaitable[Any]],
                       notification_service: UserNotificationService,
                       alert_sink: Optional[AlertSinkPort], task_dispatch: Optional[TaskDispatchService],
                       video_registry: Optional[ProviderRegistry], quota_service: Optional[Any],
                       media_storage: Optional[Any], link_service: Optional[Any], short_link_service: Optional[Any],
                       deep_research_fetch: Optional[Callable[[str], Awaitable[dict]]],
                       prompt_content_store: Optional[PromptContentStore]) -> Mapping[str, WorkerTask]:
    """Task type -> runner for every task served outside `WorkerHandler`."""
    tasks: Dict[str, WorkerTask] = {}
    if notes_port is None:
        tasks["execute_reminder"] = DisabledTask(
            "execute_reminder: notes_port not configured", 501, {"error": "notes_port not configured"})
    else:
        tasks["execute_reminder"] = ExecuteReminderTask(
            notes_port=notes_port, user_repo=user_repo, ensure_agents=ensure_agents,
            notification_service=notification_service, alert_sink=alert_sink)
    if video_registry is None or task_dispatch is None:
        tasks["video_generation_polling"] = DisabledTask(
            "video_generation_polling: video_registry or task_dispatch not configured", 500,
            {"error": "video_generation not configured"})
    else:
        tasks["video_generation_polling"] = VideoGenerationPollingTask(
            video_registry=video_registry, task_dispatch=task_dispatch, ensure_agents=ensure_agents,
            notification_service=notification_service, media_storage=media_storage, quota_service=quota_service,
            link_service=link_service, short_link_service=short_link_service, deliver_video=deliver_video)
    notice_sender = DeepResearchNoticeSender(notification_service=notification_service, alert_sink=alert_sink)
    tasks["deep_research_failed"] = DeepResearchFailedTask(ensure_agents=ensure_agents, notice_sender=notice_sender)
    if deep_research_fetch is None:
        tasks["deep_research_webhook_event"] = DisabledTask(
            "deep_research_webhook_event: OpenAI retrieval not configured", 422, {"error": "retrieval not configured"})
    else:
        tasks["deep_research_webhook_event"] = DeepResearchWebhookEventTask(
            fetch=deep_research_fetch, ensure_agents=ensure_agents, prompt_content_store=prompt_content_store,
            task_dispatch=task_dispatch, media_storage=media_storage, link_service=link_service,
            notification_service=notification_service, notice_sender=notice_sender,
            deliver_report=deliver_deep_research)
    return tasks
