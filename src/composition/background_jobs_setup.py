"""Work that runs outside a chat turn: the agent worker handler (an agent's queued result delivered to the user), the
task dispatch over the agent-task queue, the reminders on it, and the deep-research webhook's claims."""
from dataclasses import dataclass
from typing import Any, Optional

from ..adapters.firestore_dedup_store import FirestoreDedupStore
from ..config.environment import EnvironmentConfig
from ..domain.notification_kind import NotificationKind
from ..handlers.agent_worker_handler import AgentWorkerHandler
from ..infrastructure.agent_coordinator import AgentCoordinator
from ..infrastructure.notification_sla import dispatch_deadline_s
from ..services.deep_research_drive_copy_service import DeepResearchDriveCopyService
from ..services.reminders_service import RemindersService
from ..services.task_dispatch_service import TaskDispatchService
from ..services.user_notification_service import UserNotificationService
from .file_delivery_setup import FileDelivery
from .language_setup import Languages

# OpenAI's webhook retry window; liveness follows each claim's own expires_at.
_DEEP_RESEARCH_CLAIM_TTL_S = 72 * 3600


@dataclass(frozen=True)
class BackgroundJobs:
    """`task_dispatch` None: no task queue. `reminders` None: no task dispatch or no notes store."""

    agent_worker_handler: AgentWorkerHandler
    task_dispatch: Optional[TaskDispatchService]
    reminders: Optional[RemindersService]
    deep_research_dedup_store: FirestoreDedupStore


def build_background_jobs(db_client: Any, env_config: EnvironmentConfig, *, container: Any,
                          coordinator: AgentCoordinator, notification_service: UserNotificationService,
                          task_queue: Any, delivery: FileDelivery, user_repo: Any,
                          languages: Languages) -> BackgroundJobs:
    agent_worker_handler = AgentWorkerHandler(
        coordinator=coordinator,
        notification_service=notification_service,
        media_storage=delivery.media_storage,
        task_queue=task_queue,
        doc_delivery_service=delivery.doc_delivery_service,
        link_service=delivery.link_service,
        short_link_service=delivery.short_link_service,
        # Opt-in copy of deep research reports to the user's drive (decisions/deep_research_drive_copy.md).
        # Always wired: with no drive on this deployment, an opted-in user gets the "not connected" line.
        deep_research_drive_copy=DeepResearchDriveCopyService(
            users=user_repo,
            drive=container.user_drive_service,
            localization=languages.localization,
            language=languages.resolver,
        ),
    )
    task_dispatch = TaskDispatchService(task_queue) if task_queue else None
    reminders = RemindersService(
        notes_port=container.notes_adapter,
        user_repo=user_repo,
        task_dispatch=task_dispatch,
        recurrence=container.recurrence_adapter,
        # Derived from NOTIFICATION_SLA here: services cannot import the infrastructure layer (REQ-ARCH-22).
        dispatch_deadline_s=dispatch_deadline_s(NotificationKind.REMINDER),
    ) if (container.notes_adapter and task_dispatch) else None
    claims = FirestoreDedupStore(db_client=db_client, collection_name=env_config.worker_task_dedup_collection,
                                 ttl_seconds=_DEEP_RESEARCH_CLAIM_TTL_S)
    return BackgroundJobs(agent_worker_handler=agent_worker_handler, task_dispatch=task_dispatch, reminders=reminders,
                          deep_research_dedup_store=claims)
