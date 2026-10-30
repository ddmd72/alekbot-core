"""
Drive indexing wiring shared by the main service (`main.py`, `ServiceContainer`) and the indexer
service (`indexer_main.py`) (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §4, §12.5, §13).

The drive queue is a second `TaskQueue` instance bound to `drive-sync-{suffix}` and to the indexer
service's `/worker`. Its `create_queue_if_not_exists` is never called: it would create the queue with
concurrency 5, while `cloudbuild-dev.yaml` provisions it with `max-concurrent-dispatches=1` (§4).
`DRIVE_INDEXER_URL` is an optional knob read with `os.getenv` here, never registered in
`load_settings()` (CLAUDE.md, `config.get()` vs `os.getenv()`): unset, the main service sends no
nudges and runs no fan-out, and nothing else changes.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, List, Mapping, Optional

from ..adapters.firestore_drive_index_repo import FirestoreDriveIndexRepository
from ..adapters.firestore_drive_mirror_repo import FirestoreDriveMirrorRepository
from ..adapters.gcp_task_queue import GcpTaskQueue
from ..adapters.geo_place_table import load_place_rows
from ..config.environment import EnvironmentConfig
from .connector_registry import MICROSOFT_ONEDRIVE
from ..domain.drive_index_planner import PlaceOf
from ..domain.place_lookup import PlaceIndex
from ..ports.alert_sink import AlertSinkPort
from ..ports.drive_index_repository import DriveIndexRepository
from ..ports.drive_mirror_repository import DriveMirrorRepository
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_queue import TaskQueue
from ..ports.user_drive_port import UserDrivePort
from ..ports.user_repository import UserRepository
from ..services.drive_sync_scheduler_service import DriveSyncSchedulerService
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..ports.language_service_port import LanguageServicePort
    from ..services.localization_service import LocalizationService
    from ..services.user_notification_service import UserNotificationService

DRIVE_QUEUE_LOCATION = "us-central1"


def drive_queue_name(env_config: EnvironmentConfig) -> str:
    """`drive-sync-{suffix}`, the same suffix rule as `agent-tasks-{suffix}`."""
    return f"drive-sync-{'dev' if env_config.is_development else 'prod'}"


def build_drive_index_repository(db_client: Any, env_config: EnvironmentConfig) -> DriveIndexRepository:
    return FirestoreDriveIndexRepository(db_client, env_config)


def build_drive_mirror_repository(db_client: Any, env_config: EnvironmentConfig) -> DriveMirrorRepository:
    """The transcript mirror's roots and ledger (USER_DRIVE_INDEXING_RFC §27.9): read by the main service for the Step 1
    guard, the deleted-files search and the open of an archived transcript."""
    return FirestoreDriveMirrorRepository(db_client, env_config)


def build_drive_task_queue(config: Mapping[str, Any], env_config: EnvironmentConfig) -> Optional[TaskQueue]:
    """The drive queue, targeting the indexer service; None when `DRIVE_INDEXER_URL` is unset."""
    indexer_url = (os.getenv("DRIVE_INDEXER_URL") or "").strip().rstrip("/")
    project = config.get("GOOGLE_CLOUD_PROJECT")
    if not indexer_url or not project:
        logger.info("📬 Drive sync queue: disabled (DRIVE_INDEXER_URL or GOOGLE_CLOUD_PROJECT unset)")
        return None
    queue = GcpTaskQueue(
        project_id=project,
        location=DRIVE_QUEUE_LOCATION,
        queue_name=drive_queue_name(env_config),
        service_url=indexer_url,
        service_account_email=config.get("DRIVE_INVOKER_SA_EMAIL") or config.get("SERVICE_ACCOUNT_EMAIL"),
    )
    logger.info(f"📬 Drive sync queue initialized: {drive_queue_name(env_config)}")
    return queue


def build_place_lookup() -> PlaceOf:
    """Offline places from the bundled city table (§5.3, RFC Δ7; geo table v2, §25.4.3): the display name and its
    `place_forms` (PlaceIndex.place), which the planner writes together with the geo v2 stamp. Loaded once per
    process."""
    return PlaceIndex(load_place_rows()).place


def build_drive_sync_scheduler(*, repo: DriveIndexRepository, queue: TaskQueue, drive: UserDrivePort,
                               users: UserRepository, oauth_credentials: OAuthCredentialsPort,
                               notifier: Optional["UserNotificationService"],
                               alert_sink: Optional[AlertSinkPort],
                               ensure_agents: Optional[Callable[[str], Awaitable[Any]]] = None,
                               localization: Optional["LocalizationService"] = None,
                               language: Optional["LanguageServicePort"] = None,
                               ) -> DriveSyncSchedulerService:
    """The `start_drive_sync` fan-out (RFC Δ5, Δ6). The drive kind stays here, in composition (the registry's):
    the service only sees a callable listing the users with stored drive credentials. `localization` and
    `language` render the notice about files the bot cannot open (USER_DRIVE_INDEXING_RFC §26.2)."""
    def connected_users() -> Awaitable[List[str]]:
        return oauth_credentials.list_user_ids(MICROSOFT_ONEDRIVE)

    return DriveSyncSchedulerService(repo=repo, queue=queue, drive=drive, users=users,
                                     connected_users=connected_users, notifier=notifier, alert_sink=alert_sink,
                                     ensure_agents=ensure_agents, localization=localization, language=language)
