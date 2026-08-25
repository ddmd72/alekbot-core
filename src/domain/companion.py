"""
CompanionRecord — shared session-scoped memory record for the companion
agent family (tutor, group-chat moderator, ...).

One document per record (not one-doc-per-session-with-array): vector
find_nearest needs per-document granularity, so this mirrors IndexedEmail's
shape, not Session's. No SCD2 fields — session records accumulate, they do
not supersede a prior truth the way a biography (FactEntity) does.

RFC: docs/10_rfcs/COMPANION_AGENTS_RFC.md §6.
"""
from datetime import datetime, timezone
from typing import List, Optional
from uuid import uuid4

from pydantic import BaseModel, Field


class CompanionRecord(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))

    # Identity — session_id is the operative retrieval key (RFC §2, §6).
    session_id: str
    account_id: str            # billing anchor + tenancy, same convention as FactEntity
    created_by_user_id: str    # attribution

    # Content
    text: str
    vector: Optional[List[float]] = None
    tags: List[str] = Field(default_factory=list)

    # A tutor's "recurring subjunctive error" and a moderator's "volunteered
    # to bring snacks" are different values here, not different schemas —
    # each companion type defines its own vocabulary.
    domain: str

    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
