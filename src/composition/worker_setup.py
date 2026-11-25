"""Composition helper: the `/worker` task runners that live outside `WorkerHandler` (composition-root assessment §3 B).

`build_application_graph` passes the returned registry to `WorkerHandler(tasks=...)`, which merges it over the tasks it
still serves itself and refuses a key it already has. Each runner owns only its own dependencies; a feature whose
dependency is absent gets a `DisabledTask` answering the refusal the task gave before it left the handler.
"""
from typing import Any, Awaitable, Callable, Dict, Mapping, Optional, Tuple

from ..domain.worker_task import WorkerTask
from ..domain.notification_kind import NotificationKind
from ..infrastructure.notification_sla import dispatch_deadline_s
from ..ports.account_repository import AccountRepository
from ..ports.agent_note_port import AgentNotePort
from ..ports.alert_sink import AlertSinkPort
from ..ports.dedup_store import DedupStore
from ..ports.user_repository import UserRepository
from ..services.billing_daily_summary_task import BillingDailySummaryTask
from ..services.background_empty_answer_reporter import BackgroundEmptyAnswerReporter
from ..services.daily_email_review_task import DailyEmailReviewTask
from ..services.deep_research_failed_task import DeepResearchFailedTask
from ..services.deep_research_notice_sender import DeepResearchNoticeSender
from ..services.disabled_task import DisabledTask
from ..services.email_review_service import EmailReviewService
from ..services.execute_reminder_task import ExecuteReminderTask
from ..services.external_task_delivery_task import ExternalTaskDeliveryTask
from ..services.external_task_service import ExternalTaskService
from ..services.external_task_timer_task import ExternalTaskTimerTask
from ..services.external_task_wake_task import ExternalTaskWakeTask
from ..services.geo_mark_turn_task import GeoMarkTurnTask
from ..services.memory_inbox_final_task import MemoryInboxFinalTask
from ..services.memory_inbox_sweep_task import MemoryInboxSweepTask
from ..services.memory_inbox_triage_task import MemoryInboxTriageTask
from ..services.gmail_reconnect_flag import GmailReconnectFlag
from ..services.provider_registry import ProviderRegistry
from ..services.start_daily_email_review_task import StartDailyEmailReviewTask
from ..services.task_dispatch_service import TaskDispatchService
from ..services.user_notification_service import UserNotificationService
from ..services.video_generation_delivery import deliver_video
from ..services.video_generation_polling_task import VideoGenerationPollingTask
from ..services.workspace_service import WorkspaceService
from .memory_inbox_setup import MemoryInbox


def build_worker_tasks(*, notes_port: Optional[AgentNotePort], user_repo: UserRepository,
                       ensure_agents: Callable[[str], Awaitable[Any]],
                       notification_service: UserNotificationService,
                       alert_sink: Optional[AlertSinkPort], task_dispatch: Optional[TaskDispatchService],
                       video_registry: Optional[ProviderRegistry], quota_service: Optional[Any],
                       media_storage: Optional[Any], link_service: Optional[Any], short_link_service: Optional[Any],
                       email_review: Optional[EmailReviewService], daily_review_marks: Optional[DedupStore],
                       gmail_reconnect_flag: GmailReconnectFlag,
                       account_repo: Optional[AccountRepository],
                       geo_mark_turn: Optional[Callable[[dict, dict], Awaitable[Tuple[dict, int]]]] = None,
                       external_tasks: Optional[ExternalTaskService] = None,
                       workspace: Optional[WorkspaceService] = None,
                       memory_inbox: Optional[MemoryInbox] = None,
                       ) -> Mapping[str, WorkerTask]:
    """Task type -> runner for every task served outside `WorkerHandler`. `geo_mark_turn` is the platform's runner of
    a queued `$geo` question (the Slack adapter's entry); None: the task answers 501. `external_tasks` None (no task
    queue): the three external task types answer 501; `workspace` None: a delivered result gets no workspace copy.
    `memory_inbox` None (no task queue) or `account_repo` None: the three memory inbox task types answer 501."""
    tasks: Dict[str, WorkerTask] = {}
    empty_answer_reporter = BackgroundEmptyAnswerReporter(alert_sink=alert_sink)
    if notes_port is None:
        tasks["execute_reminder"] = DisabledTask(
            "execute_reminder: notes_port not configured", 501, {"error": "notes_port not configured"})
    else:
        tasks["execute_reminder"] = ExecuteReminderTask(
            notes_port=notes_port, user_repo=user_repo, ensure_agents=ensure_agents,
            notification_service=notification_service, empty_answer_reporter=empty_answer_reporter)
    if video_registry is None or task_dispatch is None:
        tasks["video_generation_polling"] = DisabledTask(
            "video_generation_polling: video_registry or task_dispatch not configured", 500,
            {"error": "video_generation not configured"})
    else:
        tasks["video_generation_polling"] = VideoGenerationPollingTask(
            video_registry=video_registry, task_dispatch=task_dispatch, ensure_agents=ensure_agents,
            notification_service=notification_service, media_storage=media_storage, quota_service=quota_service,
            link_service=link_service, short_link_service=short_link_service, deliver_video=deliver_video)
    notice_sender = DeepResearchNoticeSender(
        notification_service=notification_service, empty_answer_reporter=empty_answer_reporter)
    tasks["deep_research_failed"] = DeepResearchFailedTask(ensure_agents=ensure_agents, notice_sender=notice_sender)
    if email_review is None or task_dispatch is None:
        tasks["start_daily_email_review"] = DisabledTask(
            "start_daily_email_review: required services not configured", 501, {"error": "services not configured"})
    else:
        tasks["start_daily_email_review"] = StartDailyEmailReviewTask(
            email_review=email_review, task_dispatch=task_dispatch, user_repo=user_repo, marks=daily_review_marks,
            deadline_seconds=dispatch_deadline_s(NotificationKind.DAILY_DIGEST))
    if email_review is None:
        tasks["daily_email_review"] = DisabledTask(
            "daily_email_review: email_review_service not configured", 501,
            {"error": "email_review_service not configured"})
    else:
        tasks["daily_email_review"] = DailyEmailReviewTask(
            email_review=email_review, ensure_agents=ensure_agents, notification_service=notification_service,
            gmail_reconnect_flag=gmail_reconnect_flag, empty_answer_reporter=empty_answer_reporter)
    if alert_sink is None:
        tasks["billing_daily_summary"] = DisabledTask(
            "billing_daily_summary: billing_webhook not configured", 501, {"error": "billing_webhook not configured"})
    elif account_repo is None:
        tasks["billing_daily_summary"] = DisabledTask(
            "billing_daily_summary: account_repo not configured", 501, {"error": "account_repo not configured"})
    else:
        tasks["billing_daily_summary"] = BillingDailySummaryTask(
            billing_webhook=alert_sink, account_repo=account_repo, user_repo=user_repo)
    tasks["geo_mark_turn"] = GeoMarkTurnTask(run_turn=geo_mark_turn)
    if external_tasks is None:
        tasks["external_task_timer"] = DisabledTask(
            "external_task_timer: external task queue not configured", 501,
            {"error": "external tasks not configured"})
        tasks["external_task_deliver"] = DisabledTask(
            "external_task_deliver: external task queue not configured", 501,
            {"error": "external tasks not configured"})
        tasks["external_task_wake"] = DisabledTask(
            "external_task_wake: external task queue not configured", 501,
            {"error": "external tasks not configured"})
    else:
        tasks["external_task_timer"] = ExternalTaskTimerTask(external_tasks=external_tasks)
        tasks["external_task_deliver"] = ExternalTaskDeliveryTask(
            external_tasks=external_tasks, workspace=workspace, notification_service=notification_service,
            ensure_agents=ensure_agents, empty_answer_reporter=empty_answer_reporter)
        tasks["external_task_wake"] = ExternalTaskWakeTask(
            external_tasks=external_tasks, notification_service=notification_service)
    if memory_inbox is None or account_repo is None:
        refusal = {"error": "memory inbox not configured"}
        tasks["memory_inbox_triage"] = DisabledTask("memory_inbox_triage: memory inbox not configured", 501, refusal)
        tasks["memory_inbox_final"] = DisabledTask("memory_inbox_final: memory inbox not configured", 501, refusal)
        tasks["memory_inbox_sweep"] = DisabledTask("memory_inbox_sweep: memory inbox not configured", 501, refusal)
    else:
        tasks["memory_inbox_triage"] = MemoryInboxTriageTask(
            store=memory_inbox.store, triage_pass=memory_inbox.triage_pass, lease=memory_inbox.lease,
            task_queue=memory_inbox.task_queue, user_repo=user_repo, account_repo=account_repo,
            owner_zones=memory_inbox.owner_zones, languages=memory_inbox.languages,
            notifications=notification_service, run_timeout_s=memory_inbox.run_timeout_s)
        tasks["memory_inbox_final"] = MemoryInboxFinalTask(
            store=memory_inbox.store, final_pass=memory_inbox.final_pass, lease=memory_inbox.lease,
            task_queue=memory_inbox.task_queue, user_repo=user_repo, owner_zones=memory_inbox.owner_zones,
            profile_step=memory_inbox.profile_step, run_timeout_s=memory_inbox.run_timeout_s)
        tasks["memory_inbox_sweep"] = MemoryInboxSweepTask(
            store=memory_inbox.store, task_queue=memory_inbox.task_queue, user_repo=user_repo,
            account_repo=account_repo, owner_zones=memory_inbox.owner_zones)
    return tasks
