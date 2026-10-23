"""The janitor's world for tests (Facts S3 · T9): the evaluating fact store, a repository that refreshes the real biographical
cache, the real counters adapter on the same fake database, a recording alert sink and a movable clock."""
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, List, Optional, Set

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


class JanitorWorld:
    def __init__(self, dry_run: bool = False, with_sink: bool = True) -> None:
        self.store = FactStore(indexes=INDEXES_FILE)
        env = make_env_config()
        bio = BiographicalContextService(self.store.repo)
        self.repo = FaultyRepo(self.store.db, env, embedding_service=self.store.embedder, biographical_context_service=bio)
        self.repo.fail_on = set()
        bio.set_repository(self.repo)
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

    def daily_docs(self) -> dict:
        return self.store.db.collection(self.env.fact_lifecycle_daily_collection).docs

    def line_docs(self) -> dict:
        return self.store.db.collection(self.env.fact_lifecycle_lines_collection).docs

    async def seed(self, text: str, **over: Any) -> str:
        """A fact written through the repository; `expires_at` given without a kind is a horizon-less date decision."""
        if "expires_at" in over:
            over.setdefault("expiry_kind", ExpiryKind.DATE)
        fact = make_fact(text, **over)
        await self.repo.add_fact(fact)
        return fact.id

    async def seed_due(self, text: str, **over: Any) -> str:
        over.setdefault("expires_at", PAST)
        return await self.seed(text, **over)
