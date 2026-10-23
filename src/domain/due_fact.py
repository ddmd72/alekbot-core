from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from .entities import ExpiryKind


@dataclass(frozen=True)
class DueFact:
    """A current fact whose `expires_at` has passed, as the janitor reads it: no text, no vectors.

    `update_time` is the store's change token for the document as read; a write under it fails when the fact changed since.
    `expiry_kind` is None for a document written before the expiry model (the janitor skips it).
    """

    fact_id: str
    account_id: str
    expires_at: datetime
    expiry_kind: Optional[ExpiryKind]
    update_time: object
    lifecycle_events: List[Dict[str, Any]] = field(default_factory=list)
