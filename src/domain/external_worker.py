"""A registered external worker (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.6).

`{prefix}external_workers`, doc id `{user_id}:{worker_id}`. The label is unique among the user's active workers and
never changes; a removed worker is `revoked` and frees its label. The wake-up target (§5.8): `wake_host` is the
plaintext part the Cabinet shows and `has_wake` says a target is set; `wake_secret` is one `CipherPort` ciphertext of
`{url, headers}` (`WakeTarget.to_json`) with AAD `external_workers/{user_id}:{worker_id}` (E3), never shown again.
`wake_generation` moves on at every ping the service starts; a wake task of an older generation sends nothing.

One module on purpose: the record and its status enum.
"""
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict

from .external_task import WorkerDestination


class WorkerStatus(str, Enum):
    ACTIVE = "active"
    REVOKED = "revoked"


def worker_doc_id(user_id: str, worker_id: str) -> str:
    """The worker record's document id."""
    return f"{user_id}:{worker_id}"


class ExternalWorker(BaseModel):
    model_config = ConfigDict(frozen=True)

    worker_id: str
    user_id: str
    account_id: str
    label: str
    description: Optional[str] = None
    destination: WorkerDestination = WorkerDestination.WORKSPACE
    wake_host: Optional[str] = None
    has_wake: bool = False
    wake_secret: Optional[str] = None
    wake_generation: int = 0
    status: WorkerStatus = WorkerStatus.ACTIVE
    created_at: datetime
    updated_at: datetime

    @property
    def is_active(self) -> bool:
        return self.status is WorkerStatus.ACTIVE
