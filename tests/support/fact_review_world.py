"""The review pass's world for tests (Facts S4 · T0): the evaluating fact store with the real repository, biographical cache,
counters, memory-write lease, task queue, session store, notification service and localization, over one fake database,
plus fakes of the neighbours that carry state (notification state, channel bindings, language service, the platform
channels) and a recording alert sink.

Nothing here decides a fact rule: the fakes store and answer like their ports, and the rest is production code.
The review store and the question/state seeds join this file in the tasks that create those types (T4a1/T4b), because a
fake cannot subclass a port that does not exist yet; the interactive-message port's fake (T5a) is here.
"""
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Set, Tuple

from src.adapters.firestore_fact_lifecycle_counters import FirestoreFactLifecycleCounters
from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.adapters.firestore_session_store import FirestoreSessionStore
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.config.environment import EnvironmentConfig
from src.domain.channel_binding import ChannelBinding
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.interactive_message import InteractiveMessage
from src.domain.language import LanguageCode
from src.domain.llm import Message
from src.domain.messaging import MessageRef
from src.domain.notification import NotificationChannel
from src.ports.channel_binding_port import ChannelBindingPort
from src.ports.interactive_message_port import InteractiveMessagePort
from src.ports.language_service_port import LanguageServicePort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.services.user_notification_service import UserNotificationService
from tests.support.fact_lifecycle_world import NOW, PAST, RecordingSink, make_fact_world, seed_fact
from tests.support.recording_task_queue import make_task_queue


class ReviewSessionStore(FirestoreSessionStore):
    """The real session store on the world's fake database. `fail` makes every append fail inside the store's own
    try block (its threshold resolver raises), so the real "logged and swallowed, or raised with `raise_on_error`" path
    runs. Overflow batches (the 200-message window) are kept in `overflow`."""

    def __init__(self, db: Any, collection: str) -> None:
        super().__init__(db, collection_prefix=collection, overflow_callback=self._record_overflow,
                         threshold_resolver=self._resolve)
        self.fail = False
        self.overflow: List[Tuple[str, str, List[Message]]] = []

    async def _resolve(self, session_id: str) -> None:
        if self.fail:
            raise RuntimeError("session store unavailable")
        return None

    async def _record_overflow(self, owner: str, session_id: str, batch: List[Message]) -> None:
        self.overflow.append((owner, session_id, batch))

    async def messages(self, session_id: str) -> List[Message]:
        return list((await self.load_session(session_id)).history)


class RecordingChannel:
    """A platform channel as `UserNotificationService.notify_raw` uses it: `send_long_text` records, or raises while `fail`."""

    def __init__(self, platform: str, channel_id: str, fail: bool = False) -> None:
        self.platform = platform
        self.channel_id = channel_id
        self.fail = fail
        self.texts: List[str] = []

    async def send_long_text(self, text: str) -> None:
        if self.fail:
            raise RuntimeError("send failed")
        self.texts.append(text)


class RecordingChannelFactory(NotificationChannelFactoryPort):
    """One `RecordingChannel` per (platform, channel_id); a platform in `unconfigured` has none, as an unregistered one has."""

    def __init__(self) -> None:
        self.channels: Dict[Tuple[str, str], RecordingChannel] = {}
        self.unconfigured: Set[str] = set()
        self.fail = False

    def create(self, platform: str, channel_id: str) -> Optional[RecordingChannel]:
        if platform in self.unconfigured:
            return None
        channel = self.channels.setdefault((platform, channel_id), RecordingChannel(platform, channel_id))
        channel.fail = self.fail
        return channel

    @property
    def sent(self) -> List[Tuple[str, str, str]]:
        """(platform, channel_id, text) of every delivered text, per channel in order."""
        return [(c.platform, c.channel_id, text) for c in self.channels.values() for text in c.texts]


class RecordingInteractiveMessagePort(InteractiveMessagePort):
    """A platform that keeps what it was given. `post` numbers messages per channel and answers the ref; `show_choice`
    records the outcome (the message then has no controls), and fails for a message that was never posted, as a
    platform does. `fail` / `fail_show` make the next calls raise; `unreachable` lists platforms that refuse every post."""

    def __init__(self) -> None:
        self.posted: List[Tuple[MessageRef, InteractiveMessage]] = []
        self.shown: Dict[MessageRef, ChoiceOutcome] = {}
        self.fail = False
        self.fail_show = False
        self.unreachable: Set[str] = set()
        self._counter: Dict[Tuple[str, str], int] = {}

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        if self.fail or platform in self.unreachable:
            raise RuntimeError("post failed")
        number = self._counter.get((platform, channel_id), 0) + 1
        self._counter[(platform, channel_id)] = number
        ref = MessageRef(platform=platform, channel_id=channel_id, message_id=f"m{number}")
        self.posted.append((ref, message))
        return ref

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        if self.fail_show:
            raise RuntimeError("update failed")
        if all(posted_ref != ref for posted_ref, _ in self.posted):
            raise RuntimeError("message_not_found")
        self.shown[ref] = outcome

    def message_at(self, ref: MessageRef) -> InteractiveMessage:
        return next(message for posted_ref, message in self.posted if posted_ref == ref)

    def has_controls(self, ref: MessageRef) -> bool:
        """True while the message at `ref` still offers its options (no outcome has been shown for it)."""
        return ref not in self.shown


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
    """Per-user answers with the real service's chain: a user's preferred language is also his UI language, else the
    account default (`account_default`), else English. A user not set has no fixed language and mirror on."""

    def __init__(self) -> None:
        self.preferences: Dict[str, Tuple[Optional[LanguageCode], bool]] = {}
        self.account_default: Optional[LanguageCode] = None

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        preferred, _ = self.preferences.get(user_id, (None, True))
        return preferred or self.account_default or LanguageCode.EN

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        return self.preferences.get(user_id, (None, True))


class ReviewWorld:
    def __init__(self) -> None:
        self.store, self.repo, self.bio = make_fact_world()
        self.env = EnvironmentConfig()
        self.counters = FirestoreFactLifecycleCounters(self.store.db, self.env)
        self.lease = FirestoreMemoryWriteLease(self.store.db, self.env)
        self.queue, self.tasks = make_task_queue()
        self.localization = FileLocalizationAdapter()
        self.sink = RecordingSink()
        sessions = f"{self.env.firestore_collection_prefix}sessions"
        self.store.db.groups[sessions] = "development_sessions"      # the index file declares the sessions index under the live name
        self.session_store = ReviewSessionStore(self.store.db, sessions)
        self.notification_state = FakeNotificationState(NOW)
        self.channels = RecordingChannelFactory()
        self.interactive = RecordingInteractiveMessagePort()
        self.notifier = UserNotificationService(self.notification_state, self.channels, coordinator=None, notification_sla={})
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
        return await seed_fact(self.repo, text, **over)

    async def stale(self, fact_id: str) -> None:
        """Stale a due fact at `now` the way the sweep does: the repository's own write, then the biographical cache refreshed.
        Raises AssertionError when the fact is not due at `now`."""
        due = next((item for item in await self.repo.find_due_facts(self.now) if item.fact_id == fact_id), None)
        if due is None:
            raise AssertionError(f"fact {fact_id!r} is not due at {self.now.isoformat()}")
        await self.repo.stale_due_fact(due, self.now)
        await self.repo.refresh_biographical_context_cache(due.account_id)

    async def seed_stale(self, text: str, **over: Any) -> str:
        """A fact the janitor staled at `now`: seeded due (`expires_at` defaults to two days ago), then `stale`d."""
        over.setdefault("expires_at", PAST)
        fact_id = await self.seed(text, **over)
        await self.stale(fact_id)
        return fact_id
