"""One account's memory-review bookkeeping: when it last ran, what it has sent today, which question is open."""
from datetime import date, datetime
from typing import Dict, Optional

from pydantic import BaseModel, ConfigDict, Field


class FactReviewState(BaseModel):
    """`current_question_id` is the one question the owner may answer by reply (none when no question is out). `sent` counts
    the questions sent on the local day `sent_on`; `delivery_failed_on` is the local day a post failed (no retry that day).
    `offers` counts how often a stale fact went to the model without a decision. `message_language` and `labels` are
    the last language the review reported for the owner's messages and the texts the model wrote for it (by language)."""

    model_config = ConfigDict(frozen=True)

    account_id: str = Field(min_length=1)
    owner_user_id: Optional[str] = None
    enqueued_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    last_review_at: Optional[datetime] = None
    last_failed_at: Optional[datetime] = None
    unsuccessful_since: Optional[datetime] = None
    offers: Dict[str, int] = Field(default_factory=dict)
    last_digest_at: Optional[datetime] = None
    undigested_since: Optional[datetime] = None
    oldest_alert_at: Optional[datetime] = None
    queue_alert_at: Optional[datetime] = None
    current_question_id: Optional[str] = None
    sent_on: Optional[date] = None
    sent: int = Field(default=0, ge=0)
    delivery_failed_on: Optional[date] = None
    message_language: Optional[str] = None
    labels: Dict[str, Dict[str, str]] = Field(default_factory=dict)
