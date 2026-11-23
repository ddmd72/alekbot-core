"""FactsPin — a user's opt-in facts PIN and duress code (CABINET_DEVICE_SESSIONS_RFC §7.7).

One record per user who set a PIN (a user without one has no record: nothing to check, nothing locks them through it).
Neither code is stored: each is kept as its keyed digest (`keyed_digest(SECRET_DIGEST_KEY, "facts_pin" | "duress_pin",
f"{user_id}:{code}")`), so the 10⁴ codes cannot be tested offline without the key in Secret Manager; the real defence is
the server-side attempt limit (`FactsPinPolicy.MAX_WRONG`) that `wrong_in_row` counts. A pending reset (a forgotten PIN)
is `reset_requested_at` and `reset_due_at`; at `reset_due_at` the record is deleted and the user sets a new PIN, or none.
"""
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class FactsPin(BaseModel):
    """One user's PIN record (frozen; change it with `model_copy(update=...)`). Every datetime is tz-aware UTC.

    `wrong_in_row` counts the wrong codes since the last right one, across the Cabinet and chat. `reset_requested_at`
    and `reset_due_at` are both set while a reset is pending and both None otherwise. `updated_at` is the last write to
    the record."""

    model_config = ConfigDict(frozen=True)

    user_id: str
    pin_digest: str
    duress_digest: str
    wrong_in_row: int = 0
    reset_requested_at: Optional[datetime] = None
    reset_due_at: Optional[datetime] = None
    updated_at: datetime
