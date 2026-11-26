"""One OAuth grant of the task queue's worker resource, as the Cabinet lists it (EXTERNAL_TASK_QUEUE_RFC §5.4 "OAuth",
§5.6): which client holds it, when it was granted and when its token was last refreshed. `grant_id` is the grant's
stable id (not a secret: a stored field, never a credential); the Cabinet's Revoke names a grant by it. The memory
inbox's connected apps are listed with the same type."""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True)
class WorkerGrant:
    grant_id: str
    client_id: str
    client_name: str
    granted_at: Optional[datetime]     # None on a grant made before the time was recorded
    last_refresh: Optional[datetime]   # the live token's issue time; None likewise
