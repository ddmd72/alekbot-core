"""One absolute deadline per chat turn that may run long.

LONG_RUNNING_TURNS_RFC §5.1. The clock travels in a ContextVar so every layer of one
turn — DelegationEngine, BaseAgent, SmartResponseAgent — reads the same deadline
without threading it through message context (which is copied into specialist
messages and Cloud Task payloads). A task created inside the turn inherits it; an
ASYNC delegation running in another process does not, by design.

Marker notes are short data markers. What they mean to the model is defined in the
PROTOCOL_LONG_TURNS prompt token, not here.
"""
from __future__ import annotations

import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Awaitable, Callable, List, Optional, Set

from .llm import Message

LONG_TURN_BUDGET_S = 1500
WRAP_UP_RESERVE_S = 120
LONG_TURN_MAX_LOOP_TURNS = 40
NOTICE_AFTER_S = 90
HEARTBEAT_S = 30
STALE_AFTER_S = 90
HARD_STOP_MARGIN_S = 60

WRAP_UP_NOTE = "[System: turn budget ending — final answer now]"
MEANWHILE_HEADER = "[System: meanwhile in chat]"
RUNNING_JOBS_HEADER = "[System: still running in the background]"

_QUESTION_QUOTE_MAX = 160

FetchSince = Callable[[float], Awaitable[List[Message]]]


@dataclass
class TurnClock:
    deadline: float
    wrap_up_reserve_s: float
    max_loop_turns: int
    orchestrator_agent_type: str
    fetch_since: Optional[FetchSince] = None
    marked: bool = False
    snapshot_at: float = 0.0
    seen_until: float = 0.0
    own_created_ats: Set[float] = field(default_factory=set)
    step: str = "starting"

    @classmethod
    def start(
        cls,
        budget_s: float = LONG_TURN_BUDGET_S,
        wrap_up_reserve_s: float = WRAP_UP_RESERVE_S,
        max_loop_turns: int = LONG_TURN_MAX_LOOP_TURNS,
        orchestrator_agent_type: str = "smart_response",
        fetch_since: Optional[FetchSince] = None,
        now: Optional[float] = None,
    ) -> "TurnClock":
        start = time.monotonic() if now is None else now
        return cls(
            deadline=start + budget_s,
            wrap_up_reserve_s=wrap_up_reserve_s,
            max_loop_turns=max_loop_turns,
            orchestrator_agent_type=orchestrator_agent_type,
            fetch_since=fetch_since,
        )

    def remaining(self, now: Optional[float] = None) -> float:
        current = time.monotonic() if now is None else now
        return max(0.0, self.deadline - current)

    def call_timeout(self, now: Optional[float] = None) -> int:
        """Per-call LLM timeout: what is left minus the wrap-up reserve, at least 1 s."""
        return max(1, int(self.remaining(now) - self.wrap_up_reserve_s))

    def wrap_up_timeout(self, now: Optional[float] = None) -> int:
        return max(1, int(self.remaining(now)))

    def in_reserve(self, now: Optional[float] = None) -> bool:
        return self.remaining(now) <= self.wrap_up_reserve_s

    def can_retry(self, now: Optional[float] = None) -> bool:
        return not self.in_reserve(now)


CURRENT_TURN_CLOCK: ContextVar[Optional[TurnClock]] = ContextVar(
    "current_turn_clock", default=None
)


def render_chat_since(messages: List[Message]) -> str:
    """Summaries (`text`, never `full_text`) of messages written since the snapshot."""
    lines = [MEANWHILE_HEADER]
    for msg in messages:
        who = "you" if msg.role == "model" else "user"
        text = " ".join(p.text for p in msg.parts if p.text).strip()
        if text:
            lines.append(f"- {who}: {text}")
    return "\n".join(lines)


def render_late_answer_note(question: str, asked_at: str, link: Optional[str]) -> str:
    q = " ".join(question.split())
    if len(q) > _QUESTION_QUOTE_MAX:
        q = q[: _QUESTION_QUOTE_MAX - 1] + "…"
    where = link or "no link"
    return f'[System: late answer to "{q}" (asked {asked_at}, {where})]'
