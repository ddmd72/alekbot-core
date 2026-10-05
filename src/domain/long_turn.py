"""A chat turn that outlived the 90 s mark (LONG_RUNNING_TURNS_RFC §5.4)."""
from enum import Enum

from pydantic import BaseModel


class LongTurnStatus(str, Enum):
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RetryVerdict(str, Enum):
    """What a worker should do with an event whose turn may already exist."""
    NEW = "new"            # no record: process normally
    RUNNING = "running"    # live elsewhere: do nothing
    STALE = "stale"        # died with its instance: report once
    FINISHED = "finished"  # already answered or reported: do nothing


class LongTurnRecord(BaseModel):
    turn_id: str
    user_id: str
    session_id: str
    title: str
    started_at: float
    heartbeat_at: float
    step: str = "starting"
    status: LongTurnStatus = LongTurnStatus.RUNNING
    cancel_requested: bool = False

    def verdict(self, now: float, stale_after_s: float) -> RetryVerdict:
        if self.status is not LongTurnStatus.RUNNING:
            return RetryVerdict.FINISHED
        if now - self.heartbeat_at > stale_after_s:
            return RetryVerdict.STALE
        return RetryVerdict.RUNNING
