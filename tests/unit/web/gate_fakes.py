"""Shared pieces of the inbound-gate tests (P7): a real session service, real tokens, and a tripwire for
dependencies an unauthenticated request must never reach.

The gates under test are always the real ones (SessionService, the OIDC gate, the Twilio verifier, ...); only a
dependency that lies BEHIND a gate is replaced, and then by a `Tripwire` that records every touch, so a test can
assert that a refused request had no side effect at all."""
from datetime import datetime, timedelta, timezone
from typing import List

import jwt

from src.domain.billing import BillingAccount
from src.domain.user import UserProfile
from src.services.session_service import SessionService

SESSION_SECRET = "p7-session-secret-0123456789abcdef-xyz"
OTHER_SECRET = "p7-forger-secret-0123456789abcdef-xyzw"


def session_service() -> SessionService:
    return SessionService(secret_key=SESSION_SECRET)


def access_token(user_id: str = "user-a", account_id: str = "account-a") -> str:
    return session_service().create_access_token(
        UserProfile(user_id=user_id, account_id=account_id), BillingAccount(account_id=account_id))


def refresh_token(user_id: str = "user-a", account_id: str = "account-a") -> str:
    return session_service().create_refresh_token(
        UserProfile(user_id=user_id, account_id=account_id), BillingAccount(account_id=account_id))


def expired_token(kind: str = "access", user_id: str = "user-a", account_id: str = "account-a") -> str:
    """A token signed with the right key whose `exp` has passed."""
    iat = datetime.now(timezone.utc) - timedelta(days=2)
    return jwt.encode({"sub": user_id, "account_id": account_id, "type": kind, "iat": int(iat.timestamp()),
                       "exp": int((iat + timedelta(hours=1)).timestamp())}, SESSION_SECRET, algorithm="HS256")


def forged_token(kind: str = "access", user_id: str = "user-a", account_id: str = "account-a") -> str:
    """A well-formed, unexpired token signed with a key the service does not hold."""
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": user_id, "account_id": account_id, "type": kind, "iat": int(now.timestamp()),
                       "exp": int((now + timedelta(hours=1)).timestamp())}, OTHER_SECRET, algorithm="HS256")


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
