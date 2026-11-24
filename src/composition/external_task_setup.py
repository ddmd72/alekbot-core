"""Wiring of the external task queue (EXTERNAL_TASK_QUEUE_RFC §5.2, §5.8): the two Firestore stores, the stored-secrets
cipher and the outbound-request guard under the service.

Built in the graph, not in `ServiceContainer`: the container holds neither the `/worker` task queue, the digest key nor
the account-lock check; the graph builds all three before it gets here.

The guard's own-host list (§5.8, "The bot's own hosts are refused") is built here at boot from every deploy unit's URL
the main service is configured with: its own URL, the OAuth callbacks, the MCP resource URI, the voice relay, the drive
indexer and both sandbox services. Each is read where the rest of the code reads it (the topology, `AuthConfig`, the
settings dict, or `getenv` for the two optional knobs); no host is written in code. A custom domain counts when it is
one of those URLs.
"""
import os
from typing import Any, Callable, FrozenSet, Mapping, Optional

from ..adapters.firestore_external_task_store import FirestoreExternalTaskStore
from ..adapters.firestore_external_worker_store import FirestoreExternalWorkerStore
from ..adapters.outbound_webhook.guarded_webhook_adapter import GuardedWebhookAdapter
from ..config.auth import AuthConfig
from ..config.environment import EnvironmentConfig
from ..domain.wake_target import own_hosts_of
from ..ports.cipher_port import CipherPort
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..services.external_task_service import ExternalTaskService
from ..services.inactive_user_check import InactiveUserCheck


def wake_own_hosts(config: Mapping[str, Any], auth_config: AuthConfig, service_url: str,
                   getenv: Callable[[str], Optional[str]] = os.getenv) -> FrozenSet[str]:
    """The bot's own host names, refused as wake-up targets."""
    return own_hosts_of([
        service_url, auth_config.oauth_redirect_uri, auth_config.oauth_connect_redirect_uri,
        auth_config.mcp_resource_uri, config.get("VOICE_RELAY_STREAM_URL"), config.get("SANDBOX_PARSE_URL"),
        getenv("DRIVE_INDEXER_URL"), getenv("SANDBOX_RENDER_URL"),
    ])


def build_external_task_service(db_client: Any, env_config: EnvironmentConfig, *, task_queue: Optional[TaskQueue],
                                user_repo: UserRepository, inactive_users: InactiveUserCheck,
                                digest_key: bytes, cipher: CipherPort,
                                own_hosts: FrozenSet[str]) -> Optional[ExternalTaskService]:
    """The service over its stores; None without a task queue (a local run with no GCP project): every timer, notice
    and wake-up of the queue is a Cloud Task, so there is nothing to run it on."""
    if task_queue is None:
        return None
    return ExternalTaskService(
        FirestoreExternalTaskStore(db_client, env_config), FirestoreExternalWorkerStore(db_client, env_config),
        task_queue, user_repo, inactive_users, digest_key=digest_key, cipher=cipher,
        webhook=GuardedWebhookAdapter(own_hosts), own_hosts=own_hosts)
