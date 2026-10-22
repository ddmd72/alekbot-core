"""The orchestrator's own deadline, for the specialists it runs (DRIVE_SPECIALIST_RFC §5.6).

A process-local ContextVar holding a `time.monotonic()` value, the CURRENT_TURN_CLOCK precedent: BaseAgent sets it
for an orchestrator from that orchestrator's own timeout and clears it when an orchestrator runs without one, so each
orchestrator overwrites the outer value. It is never put in a message context, so it never rides into a Cloud Task
payload, where a monotonic value means nothing; a task created inside the run (the engine's `gather`) inherits it.
A specialist that must finish inside its caller's budget (the drive specialist) reads it.
"""
from __future__ import annotations

from contextvars import ContextVar
from typing import Optional

CURRENT_RUN_DEADLINE: ContextVar[Optional[float]] = ContextVar("current_run_deadline", default=None)
