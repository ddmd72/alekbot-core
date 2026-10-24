"""The review pass's world for tests (Facts S4 · T0): the evaluating fact store with the real repository, biographical cache,
counters, memory-write lease, task queue and localization, over one fake database, plus fakes of the neighbours that
carry state (session store, notification state, channel bindings, language service) and recorders (notifier, alert sink).

Nothing here decides a fact rule: the fakes store and answer like their ports, and the rest is production code.
The review store, the interactive-message port and the question/state seeds join this file in the tasks that create those
types (T4a1/T4b, T5a), because a fake cannot subclass a port that does not exist yet.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.config.environment import EnvironmentConfig
from src.domain.channel_binding import ChannelBinding
from src.domain.entities import ExpiryKind
from src.domain.language import LanguageCode
from src.domain.llm import Message
from src.domain.notification import NotificationChannel
from src.domain.session import Session
from src.ports.channel_binding_port import ChannelBindingPort
from src.ports.language_service_port import LanguageServicePort
from src.ports.notification_state_port import NotificationStatePort
from src.ports.session_store import SessionStore
from src.services.biographical_context_service import BiographicalContextService
from tests.support.fact_kit import make_fact
from tests.support.fact_lifecycle_world import NOW, PAST, FaultyRepo, RecordingSink
from tests.support.fact_store import INDEXES_FILE, FactStore, make_env_config
from tests.support.recording_task_queue import make_task_queue


class FakeSessionStore(SessionStore):
    """Sessions in memory. A batch append stores all of its messages or none; `fail` makes the next appends fail the way
    the Firestore store does: logged and swallowed, or raised when the caller asked for `raise_on_error`."""

    def __init__(self) -> None:
        self.sessions: Dict[str, Session] = {}
        self.owners: Dict[str, str] = {}
        self.fail = False

    def messages(self, session_id: str) -> List[Message]:
        session = self.sessions.get(session_id)
        return list(session.history) if session else []

    async def load_session(self, session_id: str) -> Session:
        return self.sessions.get(session_id) or Session(session_id=session_id)

    async def save_session(self, session_id: str, state: Session) -> None:
        self.sessions[session_id] = state

    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        await self.append_messages_batch(session_id, [message], owner_id)

    async def append_messages_batch(
        self, session_id: str, messages: List[Message], owner_id: Optional[str] = None, *, raise_on_error: bool = False,
    ) -> None:
        if self.fail:
            if raise_on_error:
                raise RuntimeError("session store unavailable")
            return
        session = self.sessions.setdefault(session_id, Session(session_id=session_id, user_id=owner_id or ""))
        for message in messages:
            session.add_message(message)
        if owner_id:
            self.owners[session_id] = owner_id

    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        ids = [sid for sid, owner in self.owners.items() if owner == owner_id]
        return ids[-1] if ids else None


class RecordingNotifier:
    """Stands for `UserNotificationService.notify_raw`: keeps what was sent; `result` is what it answers (False = not delivered)."""

    def __init__(self) -> None:
        self.sent: List[Dict[str, Any]] = []
        self.result = True

    async def notify_raw(
        self, user_id: str, account_id: str, text: str,
        channel_id_override: Optional[str] = None, platform_override: Optional[str] = None,
    ) -> bool:
        self.sent.append({"user_id": user_id, "account_id": account_id, "text": text,
                          "channel_id": channel_id_override, "platform": platform_override, "delivered": self.result})
        return self.result

    @property
    def texts(self) -> List[str]:
        return [item["text"] for item in self.sent]


class FakeNotificationState(NotificationStatePort):
    def __init__(self, now: datetime) -> None:
        self._now = now
        self.last: Dict[str, NotificationChannel] = {}
        self.primary: Dict[str, NotificationChannel] = {}

    def _channel(self, user_id: str, platform: str, channel_id: str) -> NotificationChannel:
        return NotificationChannel(user_id=user_id, platform=platform, channel_id=channel_id, updated_at=self._now)

    async def save(self, user_id: str, platform: str, channel_id: str) -> None:
        self.last[user_id] = self._channel(user_id, platform, channel_id)

    async def get(self, user_id: str) -> Optional[NotificationChannel]:
        return self.last.get(user_id)

    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        self.primary[user_id] = self._channel(user_id, platform, channel_id)

    async def get_primary(self, user_id: str) -> Optional[NotificationChannel]:
        return self.primary.get(user_id)


class FakeChannelBindings(ChannelBindingPort):
    def __init__(self) -> None:
        self.bindings: Dict[str, ChannelBinding] = {}

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        return self.bindings.get(channel_id)

    async def save(self, binding: ChannelBinding) -> None:
        self.bindings[binding.channel_id] = binding

    async def delete(self, channel_id: str) -> None:
        self.bindings.pop(channel_id, None)


class FakeLanguageService(LanguageServicePort):
    """Per-user answers; a user not set has the system defaults: no fixed language, mirror on, UI language English."""

    def __init__(self) -> None:
        self.preferences: Dict[str, Tuple[Optional[LanguageCode], bool]] = {}
        self.ui_languages: Dict[str, LanguageCode] = {}

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        return self.ui_languages.get(user_id, LanguageCode.EN)

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        return self.preferences.get(user_id, (None, True))


class ReviewWorld:
    def __init__(self) -> None:
        self.store = FactStore(indexes=INDEXES_FILE)
        bio = BiographicalContextService(self.store.repo)
        self.repo = FaultyRepo(self.store.db, make_env_config(), embedding_service=self.store.embedder,
                               biographical_context_service=bio)
        self.repo.fail_on = set()
        bio.set_repository(self.repo)
        self.bio = bio
        self.env = EnvironmentConfig()
        self.counters = FirestoreFactLifecycleCounters(self.store.db, self.env)
        self.lease = FirestoreMemoryWriteLease(self.store.db, self.env)
        self.queue, self.tasks = make_task_queue()
        self.localization = FileLocalizationAdapter()
        self.sink = RecordingSink()
        self.session_store = FakeSessionStore()
        self.notifier = RecordingNotifier()
        self.notification_state = FakeNotificationState(NOW)
        self.channel_bindings = FakeChannelBindings()
        self.language = FakeLanguageService()
        self.now = NOW

    def clock(self) -> datetime:
        return self.now

    def advance(self, **delta: float) -> datetime:
        """Move the clock forward (`days=`, `hours=`, ...); the same `now` is what `clock` hands to a service."""
        self.now = self.now + timedelta(**delta)
        return self.now

    @property
    def docs(self) -> dict:
        return self.store.docs

    @property
    def enqueued(self) -> List[Tuple[str, Dict[str, Any]]]:
        return self.tasks.worker_tasks()

    async def seed(self, text: str, **over: Any) -> str:
        """A fact written through the repository; `expires_at` given without a kind is a horizon-less date decision."""
        if "expires_at" in over:
            over.setdefault("expiry_kind", ExpiryKind.DATE)
        fact = make_fact(text, **over)
        await self.repo.add_fact(fact)
        return fact.id

    async def seed_stale(self, text: str, **over: Any) -> str:
        """A fact the janitor staled at `now`: seeded due, then staled by the repository's own write."""
        over.setdefault("expires_at", PAST)
        fact_id = await self.seed(text, **over)
        due = next(item for item in await self.repo.find_due_facts(self.now) if item.fact_id == fact_id)
        await self.repo.stale_due_fact(due, self.now)
        return fact_id
