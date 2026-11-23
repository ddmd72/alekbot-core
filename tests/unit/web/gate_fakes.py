"""Shared pieces of the inbound-gate tests (P7): a real device-session kit, real tokens, and a tripwire for
dependencies an unauthenticated request must never reach.

The gates under test are always the real ones (the device-session verifier, the OIDC gate, the Twilio verifier, ...); only a
dependency that lies BEHIND a gate is replaced, and then by a `Tripwire` that records every touch, so a test can
assert that a refused request had no side effect at all."""
from datetime import datetime, timedelta, timezone
from typing import List

import jwt

from src.web.cabinet_auth import CabinetSessionVerifier
from tests.support.cabinet_session_kit import KIT_SECRET, CabinetSessionKit

SESSION_SECRET = "p7-session-secret-0123456789abcdef-xyz"
OTHER_SECRET = "p7-forger-secret-0123456789abcdef-xyzw"

# One device-session kit for the module's helpers: `cabinet_sessions()` verifies exactly the tokens they mint.
KIT = CabinetSessionKit()


def cabinet_sessions() -> CabinetSessionVerifier:
    return KIT.verifier


def access_token(user_id: str = "user-a", account_id: str = "account-a", role: str = "viewer") -> str:
    """The access JWT of a new live device session (`role` in the account's policy)."""
    return KIT.sign_in_now(user_id, account_id, role=role).access_token


def _claims(sid: str, user_id: str, account_id: str, iat: datetime, exp: datetime) -> dict:
    return {"sub": user_id, "account_id": account_id, "type": "access", "sid": sid, "iat": int(iat.timestamp()),
            "exp": int(exp.timestamp())}


def expired_token(user_id: str = "user-a", account_id: str = "account-a") -> str:
    """A token of a live device session, signed with the right key, whose `exp` has passed."""
    iat = datetime.now(timezone.utc) - timedelta(days=2)
    sid = KIT.sign_in_now(user_id, account_id).sid
    return jwt.encode(_claims(sid, user_id, account_id, iat, iat + timedelta(hours=1)), KIT_SECRET, algorithm="HS256")


def forged_token(user_id: str = "user-a", account_id: str = "account-a") -> str:
    """A well-formed, unexpired token of a live device session signed with a key the service does not hold."""
    now = datetime.now(timezone.utc)
    sid = KIT.sign_in_now(user_id, account_id).sid
    return jwt.encode(_claims(sid, user_id, account_id, now, now + timedelta(hours=1)), OTHER_SECRET, algorithm="HS256")


class TripwireTouched(Exception):
    pass


class Tripwire:
    """A dependency behind a gate. Every method call on it is recorded in `touched` and raises, so a request that
    got past a gate it should not have leaves a trace even when the route swallows the error."""

    def __init__(self, name: str, log: List[str]) -> None:
        self._name = name
        self._log = log

    def __getattr__(self, attr: str):
        if attr.startswith("__"):
            raise AttributeError(attr)

        def touched(*args, **kwargs):
            self._log.append(f"{self._name}.{attr}")
            raise TripwireTouched(f"{self._name}.{attr}")
        return touched


class RecordingTaskQueue:
    """What was enqueued, in order. Every enqueue method of the TaskQueue port the gated routes use lands here."""

    def __init__(self) -> None:
        self.tasks: List[dict] = []

    async def enqueue_worker_task(self, task_type=None, payload=None, **kwargs):
        self.tasks.append({"task_type": task_type, "payload": payload, **kwargs})
        return "task"

    async def enqueue_slack_event(self, **kwargs):
        self.tasks.append({"task_type": "slack_event", **kwargs})
        return "task"
