"""The HTTP face of the external task queue's refusals and callers (EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6).

Pure functions for `src/web/external_task_api.py`:
- `status_for` states the status table of §5.5 for every `TaskRefusal` a worker surface can see;
- `client_address` names the caller whose unknown-key guesses are counted (§5.6).
"""
import re
from typing import Optional

from .external_task_refusal import TaskRefusal

_STATUS = {
    TaskRefusal.KEY_INVALID: 401,
    TaskRefusal.NOT_ENABLED: 403,
    TaskRefusal.ACCOUNT_LOCKED: 403,
    TaskRefusal.WORKER_REVOKED: 403,
    TaskRefusal.NOT_FOUND: 404,
    TaskRefusal.STALE_LEASE: 409,
    TaskRefusal.LEASE_EXPIRED: 409,
    TaskRefusal.RENEW_CAP: 409,
    TaskRefusal.ALREADY_SUBMITTED: 409,
    TaskRefusal.ALREADY_FINAL: 409,
    TaskRefusal.TOO_LARGE: 413,
    TaskRefusal.INVALID_TEXT: 400,
}
_ADDRESS = re.compile(r"[A-Za-z0-9.:_-]{1,64}")
UNKNOWN_ADDRESS = "unknown"   # no usable header: one shared bucket, so a caller who hides its address is limited together


def status_for(reason: TaskRefusal) -> int:
    """The HTTP status of a refusal. A reason a worker never meets (owner side, registry) is a plain 400."""
    return _STATUS.get(reason, 400)


def client_address(forwarded_for: Optional[str]) -> str:
    """The address `401` guesses are counted under: the RIGHTMOST `X-Forwarded-For` entry.

    Google's front end appends the address it saw to the header; every entry to its left was supplied by the client and
    can be forged, so none of them is read. Behind a further proxy this is that proxy's edge address, and the global
    `401` limit still bounds the flood. A missing, empty or odd-looking entry counts under one shared name.
    """
    if not forwarded_for:
        return UNKNOWN_ADDRESS
    last = forwarded_for.rsplit(",", 1)[-1].strip()
    return last if _ADDRESS.fullmatch(last) else UNKNOWN_ADDRESS
