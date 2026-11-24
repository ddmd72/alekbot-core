"""The external task queue's service over fakes, for the tests of its owner-side callers (Smart's tools, `$task send`).

The same world `tests/unit/services/test_external_task_service.py` uses: `ExternalTaskService` over the in-memory stores
of `external_task_stores.py`, the real `FirestoreUserRepository` on `EvalDb` (the flag is written into the stored user and
read fresh), the real `InactiveUserCheck`, and the real `GcpTaskQueue` over a Cloud Tasks client that records its bodies.

Alex Doe is `usr-1` (account `acc-1`), flagged, with the worker `research` unless `labels` says otherwise.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Sequence

from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.external_task import TaskOrigin
from src.domain.user import UserProfile
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

USERS = "users"
DIGEST_KEY = bytes(range(32))
USER_ID = "usr-1"
ACCOUNT_ID = "acc-1"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TITLE = "Irrigation kits"
TEXT = "compare three community-garden irrigation kits for Shelbyville"
NOTE = ("Alex Doe asked for this to choose a kit for the Shelbyville community garden; present the three kits side by "
        "side with price and sturdiness, then make a document from it.")


class ExternalTaskWorld:
    def __init__(self) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = FakeExternalTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, self.client = make_task_queue()
        self._bytes = 0
        self.service = ExternalTaskService(
            self.tasks, self.workers, self.queue, self.users, InactiveUserCheck(self.users), digest_key=DIGEST_KEY,
            clock=lambda: datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc), random_bytes=self._random_bytes)

    def _random_bytes(self, n: int) -> bytes:
        self._bytes += 1
        return bytes([self._bytes % 250 + 1]) * n

    async def seed(self, labels: Sequence[str] = ("research",), *, enabled: bool = True) -> "ExternalTaskWorld":
        for uid, acc, email in ((USER_ID, ACCOUNT_ID, "alex@example.com"), ("usr-2", "acc-2", "other@example.com")):
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=email, display_name=uid))
            await self.flag(uid, True)
        for label in labels:
            await self.service.create_worker(USER_ID, label, f"{label} worker")
        await self.flag(USER_ID, enabled)
        return self

    async def flag(self, user_id: str, on: bool) -> None:
        await self.db.collection(USERS).document(user_id).update({"config.external_tasks_enabled": on})

    def jobs(self, task_type: str) -> List[Dict[str, Any]]:
        return [body for kind, body in self.client.worker_tasks() if kind == task_type]
