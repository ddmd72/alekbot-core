from contextvars import ContextVar
from typing import Dict, Optional

_TRACE_ID_CTX: ContextVar[Optional[str]] = ContextVar("trace_id", default=None)
_SESSION_ID_CTX: ContextVar[Optional[str]] = ContextVar("session_id", default=None)
_EVENT_ID_CTX: ContextVar[Optional[str]] = ContextVar("event_id", default=None)
_USER_ID_CTX: ContextVar[Optional[str]] = ContextVar("user_id", default=None)


def set_log_context(
    trace_id: Optional[str] = None,
    session_id: Optional[str] = None,
    event_id: Optional[str] = None,
    user_id: Optional[str] = None
) -> None:
    if trace_id is not None:
        _TRACE_ID_CTX.set(trace_id)
    if session_id is not None:
        _SESSION_ID_CTX.set(session_id)
    if event_id is not None:
        _EVENT_ID_CTX.set(event_id)
    if user_id is not None:
        _USER_ID_CTX.set(user_id)


def get_log_context() -> Dict[str, Optional[str]]:
    return {
        "trace_id": _TRACE_ID_CTX.get(),
        "session_id": _SESSION_ID_CTX.get(),
        "event_id": _EVENT_ID_CTX.get(),
        "user_id": _USER_ID_CTX.get()
    }
