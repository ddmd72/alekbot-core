"""Port: where long chat turns are recorded (LONG_RUNNING_TURNS_RFC §5.4)."""
from abc import ABC, abstractmethod
from typing import List, Optional

from ..domain.long_turn import LongTurnRecord, LongTurnStatus


class LongTurnRegistry(ABC):
    """Records of chat turns that passed the 90 s mark (LONG_RUNNING_TURNS_RFC §5.4). One record per turn_id.

    Holds for every method:
    - Tenancy: get/start/heartbeat/finish are by turn_id only (turn ids come from the platform event); list_running
      and request_cancel are scoped to user_id, and another user's record is never listed or changed.
    - Times: started_at and heartbeat_at are epoch seconds; a returned record carries no storage-only field.
    - Legacy: a record with a missing or invalid field, or an unknown status, raises ValidationError on get; a running
      record with a missing or invalid field fails the whole list_running; a record with an unknown status is never
      listed.
    - Errors: backend errors propagate.
    """

    @abstractmethod
    async def start(self, record: LongTurnRecord) -> None:
        """Store the record as given (status, cancel flag included), replacing any record of the same turn_id."""

    @abstractmethod
    async def get(self, turn_id: str) -> Optional[LongTurnRecord]:
        """The record, or None when absent."""

    @abstractmethod
    async def heartbeat(self, turn_id: str, step: str) -> bool:
        """Record liveness (heartbeat_at = now) and the current step. Returns True when a cancel was requested (the
        stored flag, read after the write). Errors: a turn with no record raises the backend's not-found error (NotFound today)."""

    @abstractmethod
    async def finish(self, turn_id: str, status: LongTurnStatus) -> None:
        """Set the status; nothing else changes (the cancel flag stays). Errors: no record raises the backend's not-found error (NotFound today)."""

    @abstractmethod
    async def list_running(self, user_id: str) -> List[LongTurnRecord]:
        """The user's turns that are running and live: a heartbeat within STALE_AFTER_S of now. A record left running
        by a dead attempt drops out once its heartbeat is stale. Order: none promised. No limit."""

    @abstractmethod
    async def request_cancel(self, user_id: str, turn_id: str) -> bool:
        """Set the cancel flag on the user's own running turn and return True. False, nothing written, for an absent
        record, another user's, or one not running.

        Atomicity: read then update, no transaction: a finish landing between them leaves the flag on a finished
        record and still returns True (the heartbeat loop that reads it has ended).
        """
