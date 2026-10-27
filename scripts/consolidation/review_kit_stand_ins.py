"""The review's stand-ins for the dry-run kit (Facts S4 plan T12a, F20): everything `FactReviewService` and
`FactAnswerService` touch besides the fact store, with every write going to a recorder.

Nothing here posts to a chat platform, appends to a stored session, enqueues a Cloud Task, writes a question or a lease,
or reaches a live store. Reads that must see the real world (the owner's session, the primary channel) are seeded by the
bench from real reads, or wrap a real read-only port. Each stand-in keeps what it was given in a list the report shows.

The question store is `review_kit_store.RecordingReviewStore` (production's adapter over memory). The answer service is
the real `FactAnswerService` over the kit's `WriteRecorder`, so an owner answer runs production's rules and lands in the
overlay.
"""
import asyncio
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from scripts.consolidation.review_kit_store import RecordingReviewStore
from src.adapters.file_localization_adapter import FileLocalizationAdapter
from src.domain.agent import AgentMessage, AgentResponse
from src.domain.channel_binding import ChannelBinding
from src.domain.choice_outcome import ChoiceOutcome
from src.domain.consolidation import consolidation_agent_id
from src.domain.lease import lease_held
from src.domain.interactive_message import InteractiveMessage
from src.domain.language import LanguageCode
from src.domain.llm import Message
from src.domain.messaging import MessageRef
from src.domain.notification import NotificationChannel
from src.domain.session import SessionState
from src.ports.channel_binding_port import ChannelBindingPort
from src.ports.fact_lifecycle_counters_port import FactLifecycleCountersPort
from src.ports.fact_update_port import FactUpdatePort
from src.ports.interactive_message_port import InteractiveMessagePort
from src.ports.language_service_port import LanguageServicePort
from src.ports.memory_write_lease import MemoryWriteLeasePort
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.ports.notification_state_port import NotificationStatePort
from src.ports.repository import FactRepository
from src.ports.session_store import SessionStore
from src.services.fact_answer_service import FactAnswerService
from src.services.fact_question_texts import FactQuestionTexts
from src.services.fact_review_service import FactReviewService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from src.services.user_notification_service import UserNotificationService


class LiveEffectRefused(RuntimeError):
    """A call that would have an effect outside the kit (a task that is no worker task, a send on a platform)."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------------------------------------ lease, tasks, agents

class MemoryLease(MemoryWriteLeasePort):
    """The memory-write lease in memory: one holder per account until its expiry, the port's validation and the
    domain's `lease_held` for "still held"."""

    def __init__(self) -> None:
        self._held: Dict[str, Tuple[str, datetime]] = {}
        self.taken: List[Dict[str, Any]] = []

    async def acquire(self, account_id: str, owner: str, now: datetime, until: datetime) -> bool:
        if not account_id:
            raise ValueError("account_id is required to take the memory-write lease")
        if not owner:
            raise ValueError("owner is required to take the memory-write lease")
        if now.tzinfo is None or until.tzinfo is None:
            raise ValueError("now and until must be timezone-aware")
        if until <= now:
            raise ValueError("until must be later than now")
        held = self._held.get(account_id)
        if held is not None and lease_held(held[1], now):
            return False
        self._held[account_id] = (owner, until)
        self.taken.append({"account_id": account_id, "until": until})
        return True

    async def release(self, account_id: str, owner: str) -> None:
        held = self._held.get(account_id)
        if held is not None and held[0] == owner:
            del self._held[account_id]


class RecordingTaskQueue:
    """The task queue for a dry run: `enqueue_worker_task` is recorded (a second task of one `(task_type, dedup_key)` is
    refused with None, as Cloud Tasks refuses a name it has seen); any other enqueue raises LiveEffectRefused."""

    def __init__(self) -> None:
        self.tasks: List[Dict[str, Any]] = []
        self._names: set = set()

    async def enqueue_worker_task(
        self, task_type: str, payload: Dict[str, Any], delay_seconds: int = 0, deadline_seconds: Optional[int] = None,
        dedup_key: Optional[str] = None,
    ) -> Optional[str]:
        if dedup_key is not None:
            if (task_type, dedup_key) in self._names:
                return None
            self._names.add((task_type, dedup_key))
        self.tasks.append({
            "task_type": task_type, "payload": dict(payload), "delay_seconds": delay_seconds,
            "deadline_seconds": deadline_seconds, "dedup_key": dedup_key,
        })
        return f"kit-task-{len(self.tasks)}"

    def __getattr__(self, name: str) -> Any:
        if name.startswith("enqueue_") or name in ("purge_queue", "create_queue_if_not_exists"):
            raise LiveEffectRefused(f"the dry-run kit enqueues worker tasks only, not {name}")
        raise AttributeError(name)


class NoopAgentFactory:
    """`ensure_agents_for_user` as the review service calls it: it refreshes a cached agent, which the kit does not need."""

    def __init__(self) -> None:
        self.ensured: List[str] = []

    async def ensure_agents_for_user(self, user_id: str) -> None:
        self.ensured.append(user_id)


# ------------------------------------------------------------------------------------------------ sessions

class NoAppendSessions(SessionStore):
    """Reads come from `real` (the owner's stored sessions; None reads as empty); every append is kept in `appended` and
    shown to the run's later reads of the same session, but never stored."""

    def __init__(self, real: Optional[SessionStore] = None) -> None:
        self._real = real
        self.appended: List[Tuple[str, List[Message]]] = []

    async def load_session(self, session_id: str) -> SessionState:
        state = await self._real.load_session(session_id) if self._real is not None else SessionState(session_id=session_id)
        mine = [m for sid, batch in self.appended if sid == session_id for m in batch]
        return state.model_copy(update={"history": [*state.history, *mine]}) if mine else state

    async def save_session(self, session_id: str, state: SessionState) -> None:
        raise LiveEffectRefused("the dry-run kit does not save sessions")

    async def append_message(self, session_id: str, message: Message, owner_id: Optional[str] = None) -> None:
        self.appended.append((session_id, [message]))

    async def append_messages_batch(
        self, session_id: str, messages: List[Message], owner_id: Optional[str] = None, *, raise_on_error: bool = False,
    ) -> None:
        self.appended.append((session_id, list(messages)))

    async def get_latest_session_id(self, owner_id: str) -> Optional[str]:
        return await self._real.get_latest_session_id(owner_id) if self._real is not None else None


# ------------------------------------------------------------------------------------------------ chat surface

class RecordingInteractive(InteractiveMessagePort):
    """A platform that keeps what it was given. `post` numbers messages per channel; `show_choice` records the outcome
    and never fails, even for a message the kit did not post (a question asked live earlier)."""

    def __init__(self) -> None:
        self.posted: List[Tuple[MessageRef, InteractiveMessage]] = []
        self.shown: List[Tuple[MessageRef, ChoiceOutcome]] = []
        self._counter: Dict[Tuple[str, str], int] = {}

    async def post(self, platform: str, channel_id: str, message: InteractiveMessage) -> MessageRef:
        number = self._counter.get((platform, channel_id), 0) + 1
        self._counter[(platform, channel_id)] = number
        ref = MessageRef(platform=platform, channel_id=channel_id, message_id=f"kit-{number}")
        self.posted.append((ref, message))
        return ref

    async def show_choice(self, ref: MessageRef, outcome: ChoiceOutcome) -> None:
        self.shown.append((ref, outcome))


class KitNotificationState(NotificationStatePort):
    """The last and primary channel per user, in memory; seed it from the owner's real channel."""

    def __init__(self, clock: Callable[[], datetime] = _utcnow) -> None:
        self._clock = clock
        self.last: Dict[str, NotificationChannel] = {}
        self.primary: Dict[str, NotificationChannel] = {}

    def _channel(self, user_id: str, platform: str, channel_id: str) -> NotificationChannel:
        return NotificationChannel(user_id=user_id, platform=platform, channel_id=channel_id, updated_at=self._clock())

    async def save(self, user_id: str, platform: str, channel_id: str) -> None:
        self.last[user_id] = self._channel(user_id, platform, channel_id)

    async def get(self, user_id: str) -> Optional[NotificationChannel]:
        return self.last.get(user_id)

    async def save_primary(self, user_id: str, platform: str, channel_id: str) -> None:
        self.primary[user_id] = self._channel(user_id, platform, channel_id)

    async def get_primary(self, user_id: str) -> Optional[NotificationChannel]:
        return self.primary.get(user_id)


class KitChannelBindings(ChannelBindingPort):
    """Channel bindings in memory (none by default: no channel is bound to an agent)."""

    def __init__(self) -> None:
        self.bindings: Dict[str, ChannelBinding] = {}

    async def get(self, channel_id: str) -> Optional[ChannelBinding]:
        return self.bindings.get(channel_id)

    async def save(self, binding: ChannelBinding) -> None:
        self.bindings[binding.channel_id] = binding

    async def delete(self, channel_id: str) -> None:
        self.bindings.pop(channel_id, None)


class KitLanguagePreferences(LanguageServicePort):
    """The owner's language setting in memory: per user `(fixed language or None, mirror)`; the UI language is the fixed
    one, else `ui_default`. A user not set has no fixed language and mirror on."""

    def __init__(self, ui_default: LanguageCode = LanguageCode.EN) -> None:
        self.preferences: Dict[str, Tuple[Optional[LanguageCode], bool]] = {}
        self.ui_default = ui_default

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        preferred, _ = self.preferences.get(user_id, (None, True))
        return preferred or self.ui_default

    async def get_preference(self, user_id: str) -> Tuple[Optional[LanguageCode], bool]:
        return self.preferences.get(user_id, (None, True))


class _NoChannels(NotificationChannelFactoryPort):
    """No platform is configured, so no notification can be delivered."""

    def create(self, platform: str, channel_id: str) -> None:
        return None


class NoPostNotifier(UserNotificationService):
    """`UserNotificationService` whose texts are kept, not sent: `notify_raw` resolves the channel as production does
    (over the kit's notification state) and records the text. Its channel factory has no platform, so nothing it inherits
    can deliver either."""

    def __init__(self, state: NotificationStatePort) -> None:
        super().__init__(state, _NoChannels(), coordinator=None, notification_sla={})    # type: ignore[arg-type]
        self.texts: List[Dict[str, str]] = []

    async def notify_raw(
        self, user_id: str, account_id: str, text: str, channel_id_override: Optional[str] = None,
        platform_override: Optional[str] = None,
    ) -> bool:
        channel = await self.resolve_channel(user_id, channel_id_override, platform_override)
        if channel is None:
            return False
        self.texts.append({"platform": channel.platform, "channel_id": channel.channel_id, "text": text})
        return True


# ------------------------------------------------------------------------------------------------ the bundle

@dataclass
class ReviewStandIns:
    """Every port the review service and the answer service take besides the fact store, built for one kit."""

    store: RecordingReviewStore
    lease: MemoryLease
    task_queue: RecordingTaskQueue
    agent_factory: NoopAgentFactory
    sessions: NoAppendSessions
    interactive: RecordingInteractive
    notification_state: KitNotificationState
    channel_bindings: KitChannelBindings
    language: KitLanguagePreferences
    notifier: NoPostNotifier
    texts: FactQuestionTexts
    owner_zones: OwnerTimezoneResolver
    answer_service: FactAnswerService
    extra: Dict[str, Any] = field(default_factory=dict)
    clock: Callable[[], datetime] = _utcnow      # the run's one clock: the answers, the review service and `stage_stale(at=)` read it

    def report(self) -> Dict[str, Any]:
        """What the run did to each stand-in, for the report."""
        questions = self.store.questions()
        return {
            "store_writes": list(self.store.writes),
            "questions": [
                {"question_id": q.question_id, "fact_id": q.fact_id, "status": q.status.value,
                 "answer": q.answer.value if q.answer else None, "closed_by": q.closed_by.value if q.closed_by else None}
                for q in questions
            ],
            "tasks": list(self.task_queue.tasks),
            "posts": [{"ref": asdict(ref), "prompt": message.prompt} for ref, message in self.interactive.posted],
            "choices_shown": len(self.interactive.shown),
            "texts": list(self.notifier.texts),
            "session_appends": [{"session_id": sid, "messages": len(batch)} for sid, batch in self.sessions.appended],
            "leases": [{"account_id": t["account_id"]} for t in self.lease.taken],
        }


def build_review_stand_ins(
    *,
    repo: FactRepository,
    fact_updates: FactUpdatePort,
    counters: FactLifecycleCountersPort,
    account_repo: Any,
    user_repo: Any,
    live_sessions: Optional[SessionStore] = None,
    system_default: LanguageCode = LanguageCode.EN,
    clock: Callable[[], datetime] = _utcnow,
) -> ReviewStandIns:
    """The stand-ins around `repo` (the kit's overlay store) and `fact_updates` (the kit's `WriteRecorder`), with the real
    `FactAnswerService` over them. `account_repo` and `user_repo` are read-only ports for the owner's zone; `live_sessions`
    gives the owner's stored sessions to read."""
    store = RecordingReviewStore()
    interactive = RecordingInteractive()
    sessions = NoAppendSessions(live_sessions)
    queue = RecordingTaskQueue()
    language = KitLanguagePreferences()
    notification_state = KitNotificationState(clock)
    owner_zones = OwnerTimezoneResolver(account_repo, user_repo)
    texts = FactQuestionTexts(language, FileLocalizationAdapter(), store, counters, system_default, clock)
    answers = FactAnswerService(
        fact_updates, repo, store, counters, queue, sessions, texts, interactive, owner_zones, clock,   # type: ignore[arg-type]
    )
    return ReviewStandIns(
        store=store, lease=MemoryLease(), task_queue=queue, agent_factory=NoopAgentFactory(), sessions=sessions,
        interactive=interactive, notification_state=notification_state, channel_bindings=KitChannelBindings(),
        language=language, notifier=NoPostNotifier(notification_state), texts=texts, owner_zones=owner_zones,
        answer_service=answers, clock=clock,
    )


# ------------------------------------------------------------------------------------------------ the review service

# The digest's invoices block links here; nothing is sent in a kit run, and the .invalid name resolves nowhere.
KIT_CABINET_URL = "https://cabinet.invalid/cabinet"


class KitCoordinator:
    """The coordinator the review service routes its pass through on the kit: the kit-installed `ConsolidationAgent`'s
    `execute`, nothing else. Never the real `AgentCoordinator` (it resolves the user's cached live agent, whose repository
    and fact ports write live) and never `process()` (its `_flush_billing` writes the account's live usage counters). The
    agent's timeout is the message's, as the lease budget sets it; an overrun raises, which the service takes as the pass's
    failure. `routed` keeps every message it was given, `responses` every answer."""

    def __init__(self, agent: Any) -> None:
        self._agent = agent
        self.routed: List[AgentMessage] = []
        self.responses: List[AgentResponse] = []   # what each routed message was answered, for the bench's report

    async def route_message(self, message: AgentMessage) -> AgentResponse:
        response = await self._route(message)
        self.responses.append(response)
        return response

    async def _route(self, message: AgentMessage) -> AgentResponse:
        self.routed.append(message)
        expected = consolidation_agent_id(message.context.get('user_id'))
        if message.recipient != expected:
            return AgentResponse.failure(
                message.task_id, "kit_coordinator", f"the kit routes to {expected} only, not {message.recipient}",
            )
        timeout = message.timeout_ms / 1000 if message.timeout_ms else None
        return await asyncio.wait_for(self._agent.execute(message), timeout)


def build_review_service(
    kit: Any,
    bundle: ReviewStandIns,
    *,
    agent: Any,
    account_repo: Any,
    user_repo: Any,
    run_timeout_s: Optional[float] = None,
) -> FactReviewService:
    """The real `FactReviewService` built from the kit and its stand-ins only, so no port can be a live one by mistake:
    the kit's overlay store, counters and alert sink, and the bundle's question store, lease, queue, sessions, platform,
    notifier, notification state, channel bindings, texts, owner zones, answer service, agent factory and clock (the one
    clock of the run: the bench passes the same to `stage_stale(at=)`). The two read-only ports are the owner's account and
    user repositories (the owner check and his zone). The coordinator is a `KitCoordinator` over `agent`, which must have
    the kit installed. Raises ValueError for a bundle that is not the kit's or an agent the kit was not installed on."""
    from src.infrastructure.agent_config import CONSOLIDATION

    if kit.repo is None or kit.review_world is not bundle:
        raise ValueError("the stand-ins are not the kit's: build them with kit.review_stand_ins after kit.install")
    if getattr(agent, "_repo", None) is not kit.repo:
        raise ValueError("the agent has not the kit installed: its pass would write through its own repository")
    return FactReviewService(
        repo=kit.repo,
        review_store=bundle.store,
        counters=kit.counters,
        lease=bundle.lease,
        coordinator=KitCoordinator(agent),
        agent_factory=bundle.agent_factory,
        user_repo=user_repo,
        account_repo=account_repo,
        texts=bundle.texts,
        owner_zones=bundle.owner_zones,
        notification_state=bundle.notification_state,
        channel_bindings=bundle.channel_bindings,
        notifications=bundle.notifier,
        session_store=bundle.sessions,
        task_queue=bundle.task_queue,
        answer_service=bundle.answer_service,
        interactive=bundle.interactive,
        alert_sink=kit.sink,
        fact_updates=kit.recorder,   # the kit's typed path: a history fallback or a fold's supersede lands in the overlay
        cabinet_url=KIT_CABINET_URL,
        run_timeout_s=run_timeout_s if run_timeout_s is not None else CONSOLIDATION.timeout_ms / 1000,
        clock=bundle.clock,
    )
