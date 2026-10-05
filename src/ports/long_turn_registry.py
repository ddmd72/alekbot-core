"""Port: where long chat turns are recorded (LONG_RUNNING_TURNS_RFC §5.4)."""
from abc import ABC, abstractmethod
from typing import List, Optional

from ..domain.long_turn import LongTurnRecord, LongTurnStatus


class LongTurnRegistry(ABC):
    @abstractmethod
    async def start(self, record: LongTurnRecord) -> None: ...

    @abstractmethod
    async def get(self, turn_id: str) -> Optional[LongTurnRecord]: ...

    @abstractmethod
    async def heartbeat(self, turn_id: str, step: str) -> bool:
        """Record liveness and the current step. Returns True when a cancel was requested."""

    @abstractmethod
    async def finish(self, turn_id: str, status: LongTurnStatus) -> None: ...

    @abstractmethod
    async def list_running(self, user_id: str) -> List[LongTurnRecord]:
        """The user's turns that are running and live (heartbeat not stale)."""

    @abstractmethod
    async def request_cancel(self, user_id: str, turn_id: str) -> bool:
        """Set the cancel flag on the user's own running turn. False if not found / not theirs / not running."""
