"""Wiring of the external task queue (EXTERNAL_TASK_QUEUE_RFC §5.2): the two Firestore stores under the service.

Built in the graph, not in `ServiceContainer`: the container holds neither the `/worker` task queue, the digest key nor
the account-lock check; the graph builds all three before it gets here.
"""
from typing import Any, Optional

from ..adapters.firestore_external_task_store import FirestoreExternalTaskStore
from ..adapters.firestore_external_worker_store import FirestoreExternalWorkerStore
from ..config.environment import EnvironmentConfig
from ..ports.task_queue import TaskQueue
from ..ports.user_repository import UserRepository
from ..services.external_task_service import ExternalTaskService
from ..services.inactive_user_check import InactiveUserCheck


def build_external_task_service(db_client: Any, env_config: EnvironmentConfig, *, task_queue: Optional[TaskQueue],
                                user_repo: UserRepository, inactive_users: InactiveUserCheck,
                                digest_key: bytes) -> Optional[ExternalTaskService]:
    """The service over its stores; None without a task queue (a local run with no GCP project): every timer and
    notice of the queue is a Cloud Task, so there is nothing to run it on."""
    if task_queue is None:
        return None
    return ExternalTaskService(
        FirestoreExternalTaskStore(db_client, env_config), FirestoreExternalWorkerStore(db_client, env_config),
        task_queue, user_repo, inactive_users, digest_key=digest_key)
