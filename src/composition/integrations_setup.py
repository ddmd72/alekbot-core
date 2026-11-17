"""The user's connected integrations (Gmail, Microsoft To Do, the drives) beyond what the container builds: the To Do
setup, the drive index fan-out, reconnect notices (SECRETS_AT_REST_RFC §6.2, plan D-P4), the connect flow and the
Cabinet's credential removal (MULTI_DRIVE_RFC §4.3, §4.4, §5.2), and Gmail's reconnect flag."""
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from ..config.environment import EnvironmentConfig
from ..services.credential_revocation_service import CredentialRevocationService
from ..services.drive_disconnect_service import DriveDisconnectService
from ..services.drive_sync_scheduler_service import DriveSyncSchedulerService
from ..services.gmail_reconnect_flag import GmailReconnectFlag
from ..services.integration_reconnect_service import IntegrationReconnectService
from ..services.oauth_connect_service import OAuthConnectService
from ..services.task_setup_service import TaskSetupService
from ..services.user_notification_service import UserNotificationService
from .boot_setup import BootChecks
from .connector_registry import GMAIL, MICROSOFT_TODO
from .drive_connection_setup import build_drive_activation
from .drive_indexing_setup import build_drive_sync_scheduler
from .language_setup import Languages
from .oauth_connect_setup import build_connect_activations, build_oauth_connect_service
from .operations_setup import Operations


@dataclass(frozen=True)
class Integrations:
    """`task_setup` None: no To Do client or no task queue. `drive_sync_scheduler` None: no drive queue (DRIVE_INDEXER_URL
    unset). `drive_disconnect` None: no drive is wired on this deployment."""

    task_setup: Optional[TaskSetupService]
    drive_sync_scheduler: Optional[DriveSyncSchedulerService]
    reconnect: IntegrationReconnectService
    credential_revocation: CredentialRevocationService
    drive_disconnect: Optional[DriveDisconnectService]
    connect: OAuthConnectService
    gmail_reconnect_flag: GmailReconnectFlag


def build_integrations(db_client: Any, env_config: EnvironmentConfig, *, container: Any, boot: BootChecks,
                       ops: Operations, user_repo: Any, notification_service: UserNotificationService,
                       languages: Languages, ensure_agents: Callable[[str], Awaitable[Any]]) -> Integrations:
    """Over the container's credential store, connectors and drive services. `ensure_agents` is the agent factory's
    (a reconnect or a new drive refreshes the user's agents)."""
    credentials, connectors, queue = container.oauth_credentials, container.connectors, ops.task_queue
    task_setup = None
    if container.ms_todo_adapter and queue:
        task_setup = TaskSetupService(
            lifecycle=container.ms_todo_adapter,
            task_config=container.task_config_repo,
            tasks_provider=container.ms_todo_adapter,
            oauth_credentials=credentials,
            task_search_index=container.task_search_index,
            task_queue=queue,
            notification_url_base=boot.deploy_topology.service_url,
            connector=connectors.get(MICROSOFT_TODO),
        )

    # Its ticks run on the indexer service, so it is built only when the drive queue exists.
    drive_sync_scheduler = None
    if container.drive_task_queue and container.drive_index_repo and container.drive_connection_repo:
        drive_sync_scheduler = build_drive_sync_scheduler(
            repo=container.drive_index_repo,
            queue=container.drive_task_queue,
            connections=container.drive_connection_repo,
            connection_service=container.drive_connection_service,
            notifier=notification_service,
            alert_sink=ops.alert_sink,
            ensure_agents=ensure_agents,
            localization=languages.localization,
            language=languages.resolver,
        )

    # The hourly `reconnect_notice_sweep` covers Gmail and To Do; Drive's notice is the drive fan-out's.
    reconnect = IntegrationReconnectService(
        credentials=credentials,
        users=user_repo,
        notifier=notification_service,
        email_states=container.indexed_email_repo,
        task_config=container.task_config_repo,
        drive_connections=container.drive_connection_service,
        alert_sink=ops.alert_sink,
        ensure_agents=ensure_agents,
        mail_connector=connectors.get(GMAIL),
        todo_connector=connectors.get(MICROSOFT_TODO),
    )

    # One revocation service (the provider-side revoke rule, also the Cabinet's disconnects) and one connect service
    # with one activation per integration; the drive one creates or reconnects the drive connection.
    revocation = CredentialRevocationService(credentials, connectors.edges())
    drive_wired = container.drive_connection_service is not None and container.user_drive is not None
    drive_disconnect = (
        DriveDisconnectService(container.drive_connection_service, revocation, container.user_drive,
                               container.drive_note_repo, container.drive_task_queue)
        if drive_wired else None)
    drive_activation = (
        build_drive_activation(connections=container.drive_connection_service, drive=container.user_drive,
                               task_queue=container.drive_task_queue)
        if drive_wired else None)
    connect = build_oauth_connect_service(
        db_client, env_config,
        connectors=connectors,
        credentials=credentials,
        cipher=boot.cipher,
        digest_key=boot.digest_key,
        revocation=revocation,
        activations=build_connect_activations(
            reconnect=reconnect,
            todo_connector=connectors.get(MICROSOFT_TODO),
            task_queue=queue,
            drive_activation=drive_activation,
        ),
        redirect_uri=boot.auth_config.oauth_connect_redirect_uri,
    )
    gmail_reconnect_flag = GmailReconnectFlag(
        oauth_credentials=credentials, mail_connector=connectors.get(GMAIL), ensure_agents=ensure_agents,
        notification_service=notification_service)
    return Integrations(task_setup=task_setup, drive_sync_scheduler=drive_sync_scheduler, reconnect=reconnect,
                        credential_revocation=revocation, drive_disconnect=drive_disconnect, connect=connect,
                        gmail_reconnect_flag=gmail_reconnect_flag)
