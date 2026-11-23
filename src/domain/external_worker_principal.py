"""Who a worker-side call acts for (EXTERNAL_TASK_QUEUE_RFC §5.7).

Every worker-side call resolves one principal `(user_id, account_id, worker_id)` from a verified credential: a worker
key's record (`ExternalTaskService.resolve_key`) or, later, a verified OAuth token's `sub`, `account_id` and `wid`. No
surface builds one from request input, and no worker operation takes a user, an account, a worker or a label as an
argument: it takes this object.
"""
from dataclasses import dataclass
from typing import Optional

from .external_task import SubmitVia


@dataclass(frozen=True)
class WorkerPrincipal:
    user_id: str
    account_id: str
    worker_id: str
    via: SubmitVia
    client_id: Optional[str] = None    # the OAuth client of a token; None for a key
