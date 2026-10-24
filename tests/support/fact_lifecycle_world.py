"""The janitor's world for tests (Facts S3 · T9): the evaluating fact store, a repository that refreshes the real biographical
cache, the real counters adapter on the same fake database, a recording alert sink and a movable clock."""
from datetime import datetime, timedelta, timezone
from functools import cached_property
from typing import Any, Awaitable, Callable, List, Optional, Set, Tuple

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.adapters.firestore_repo import FirestoreFactRepository
from src.config.environment import EnvironmentConfig
from src.domain.due_fact import DueFact
from src.domain.entities import ExpiryKind
from src.ports.alert_sink import AlertSinkPort
from src.services.biographical_context_service import BiographicalContextService
from src.services.fact_lifecycle_sweep_service import FactLifecycleSweepService
from tests.support.fact_kit import make_fact
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
PAST = NOW - timedelta(days=2)
FUTURE = NOW + timedelta(days=30)


class RecordingSink(AlertSinkPort):
    """Keeps what was posted; can fail, and can look at the world at the moment of the post."""

    def __init__(self) -> None:
        self.texts: List[str] = []
        self.fail = False
        self.at_post: Optional[Callable[[], Awaitable[None]]] = None

    async def post(self, text: str) -> None:
        if self.at_post is not None:
            await self.at_post()
        if self.fail:
            raise RuntimeError("webhook down")
        self.texts.append(text)


class FaultyRepo(FirestoreFactRepository):
    """The real repository, with a store fault injected at `stale_due_fact` for chosen fact ids."""

    fail_on: Set[str]

    async def stale_due_fact(self, due: DueFact, now: datetime) -> None:
        if due.fact_id in self.fail_on:
            raise RuntimeError("store unavailable")
        await super().stale_due_fact(due, now)


def make_fact_world() -> Tuple[FactStore, FaultyRepo, BiographicalContextService]:
    """The evaluating fact store (index refusal on), a repository over it that refreshes the real biographical cache, and
    that service; `repo.fail_on` names the fact ids whose staling fails."""
    store = FactStore(indexes=INDEXES_FILE)
    bio = BiographicalContextService(store.repo)
    repo = FaultyRepo(store.db, make_env_config(), embedding_service=store.embedder, biographical_context_service=bio)
    repo.fail_on = set()
    bio.set_repository(repo)
    return store, repo, bio


async def seed_fact(repo: FirestoreFactRepository, text: str, **over: Any) -> str:
    """A fact written through the repository; `expires_at` given without a kind is a horizon-less date decision."""
    if "expires_at" in over:
        over.setdefault("expiry_kind", ExpiryKind.DATE)
    fact = make_fact(text, **over)
    await repo.add_fact(fact)
    return fact.id


class JanitorWorld:
    def __init__(self, dry_run: bool = False, with_sink: bool = True) -> None:
        self.store, self.repo, _ = make_fact_world()
        self.env = EnvironmentConfig()
        self.counters = FirestoreFactLifecycleCounters(self.store.db, self.env)
        self.sink = RecordingSink()
        self.now = NOW
        self.service = FactLifecycleSweepService(
            self.repo, self.counters, self.sink if with_sink else None, dry_run, clock=lambda: self.now,
        )

    @property
    def docs(self) -> dict:
        return self.store.docs

    @cached_property
    def _queue_pair(self) -> tuple:
        from tests.support.recording_task_queue import make_task_queue     # on use: most janitor tests never load the Cloud Tasks client
        return make_task_queue()

    @property
    def queue(self) -> Any:
        """The real `GcpTaskQueue` over a client that refuses a task name it has seen (built on first use)."""
        return self._queue_pair[0]

    @property
    def tasks(self) -> Any:
        """That queue's Cloud Tasks client: `worker_tasks()` lists the created tasks as (task_type, body)."""
        return self._queue_pair[1]

    def daily_docs(self) -> dict:
        return self.store.db.collection(self.env.fact_lifecycle_daily_collection).docs

    def line_docs(self) -> dict:
        return self.store.db.collection(self.env.fact_lifecycle_lines_collection).docs

    async def seed(self, text: str, **over: Any) -> str:
        return await seed_fact(self.repo, text, **over)

    async def seed_due(self, text: str, **over: Any) -> str:
        over.setdefault("expires_at", PAST)
        return await self.seed(text, **over)
