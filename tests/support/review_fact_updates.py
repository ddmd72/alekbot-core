"""A `FactUpdatePort` for the review service's tests: it writes through a real port (the review world's own
`FactManagementAdapter`, over its evaluating Firestore fake) and keeps every call. A test scripts what a call comes to
before it reaches the real write: an exception, a refusal, or any outcome; an unscripted call is the real write."""
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable, Deque, List, Optional, Union
from collections import deque

from src.domain.fact_update import ParsedFactUpdate
from src.domain.fact_update_outcome import FactUpdateOutcome, UpdateRefusal
from src.domain.fact_write_context import FactWriteContext
from src.ports.fact_update_port import FactUpdatePort

Scripted = Union[BaseException, FactUpdateOutcome]


@dataclass(frozen=True)
class UpdateCall:
    fact_id: str
    update: ParsedFactUpdate
    account_id: str
    context: FactWriteContext
    evidence_at: Optional[datetime]
    reason: Optional[str]
    history: Optional[str]
    expected_id: Optional[str] = None


class RecordingFactUpdates(FactUpdatePort):
    """`calls` holds every call in order. `script(...)` queues what the next calls come to (first in, first out); `None`
    lets that call reach the real port."""

    def __init__(self, real: FactUpdatePort) -> None:
        self._real = real
        self.calls: List[UpdateCall] = []
        self._script: Deque[Optional[Scripted]] = deque()
        self.before: Optional[Callable[[str], Awaitable[None]]] = None   # runs once, just before the next call is applied

    def script(self, *outcomes: Optional[Scripted]) -> None:
        self._script.extend(outcomes)

    def refuse_next(self, fact_id: str, refusal: UpdateRefusal, text: str = "scripted refusal") -> None:
        self._script.append(FactUpdateOutcome.refused(fact_id, refusal, text))

    async def apply_fact_update(
        self,
        fact_id: str,
        update: ParsedFactUpdate,
        account_id: str,
        *,
        context: FactWriteContext,
        evidence_at: Optional[datetime] = None,
        reason: Optional[str] = None,
        history: Optional[str] = None,
        expected_id: Optional[str] = None,
    ) -> FactUpdateOutcome:
        self.calls.append(UpdateCall(fact_id, update, account_id, context, evidence_at, reason, history, expected_id))
        if self.before is not None:
            hook, self.before = self.before, None
            await hook(fact_id)
        scripted = self._script.popleft() if self._script else None
        if isinstance(scripted, BaseException):
            raise scripted
        if scripted is not None:
            return scripted
        return await self._real.apply_fact_update(
            fact_id, update, account_id, context=context, evidence_at=evidence_at, reason=reason, history=history,
            expected_id=expected_id,
        )
