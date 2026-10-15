"""Work-unit scopes for memory telemetry (decisions/memory_telemetry.md).

``with memory_scope(SCOPE_CHAT_TURN, "slack"):`` brackets a unit of work. Until the telemetry
service installs its hooks (tests, local runs, ``MEMORY_TELEMETRY=off``) the context manager does
nothing, so call sites need no guard — the same shape as ``set_request_context``.

Container memory is process-wide, so concurrent scopes share it. Each scope therefore records the
usage at its start, at its end and the highest usage any sampler tick saw while it was open, plus
how many scopes overlapped at the busiest moment.
"""
import asyncio
import itertools
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Protocol

from src.domain.memory_telemetry import ActiveScope, ScopeRecord
from src.utils.logging_context import get_log_context


class ScopeHooks(Protocol):
    def usage_bytes(self) -> Optional[int]: ...
    def on_scope_end(self, record: ScopeRecord) -> None: ...


@dataclass
class _Open:
    kind: str
    name: str
    trace_id: Optional[str]
    user_id: Optional[str]
    started: float
    usage_start: Optional[int]
    usage_peak: Optional[int]
    concurrent_peak: int = 1
    scope_id: int = field(default=0)


_lock = threading.Lock()
_open: Dict[int, _Open] = {}
_hooks: Optional[ScopeHooks] = None
_ids = itertools.count(1)


def install_hooks(hooks: ScopeHooks) -> None:
    global _hooks
    _hooks = hooks


def uninstall_hooks() -> None:
    global _hooks
    _hooks = None
    with _lock:
        _open.clear()


def active_scopes() -> List[ActiveScope]:
    now = time.monotonic()
    with _lock:
        return [ActiveScope(s.kind, s.name, s.trace_id, now - s.started) for s in _open.values()]


def note_usage(usage_bytes: Optional[int]) -> None:
    """Called by the sampler on every tick: raise the peak of every scope that is open."""
    if usage_bytes is None:
        return
    with _lock:
        for s in _open.values():
            if s.usage_peak is None or usage_bytes > s.usage_peak:
                s.usage_peak = usage_bytes


@contextmanager
def memory_scope(
    kind: str,
    name: str,
    trace_id: Optional[str] = None,
    user_id: Optional[str] = None,
) -> Iterator[None]:
    hooks = _hooks
    if hooks is None:
        yield
        return

    ctx = get_log_context()
    usage = hooks.usage_bytes()
    state = _Open(
        kind=kind, name=name,
        trace_id=trace_id or ctx.get("trace_id"),
        user_id=user_id or ctx.get("user_id"),
        started=time.monotonic(), usage_start=usage, usage_peak=usage,
        scope_id=next(_ids),
    )
    with _lock:
        _open[state.scope_id] = state
        n = len(_open)
        for s in _open.values():
            s.concurrent_peak = max(s.concurrent_peak, n)

    outcome = "ok"
    try:
        yield
    except asyncio.CancelledError:
        outcome = "cancelled"
        raise
    except Exception:
        outcome = "error"
        raise
    finally:
        end = hooks.usage_bytes()
        with _lock:
            _open.pop(state.scope_id, None)
        peak = state.usage_peak
        if end is not None and (peak is None or end > peak):
            peak = end
        hooks.on_scope_end(ScopeRecord(
            kind=state.kind, name=state.name, trace_id=state.trace_id,
            duration_s=time.monotonic() - state.started,
            usage_start_bytes=state.usage_start, usage_end_bytes=end, usage_peak_bytes=peak,
            concurrent_peak=state.concurrent_peak, outcome=outcome, user_id=state.user_id,
        ))
