"""The redelivery world (P15 part 2, card A): handler-level tests of a `/worker` task delivered more than once.

What is real: `WorkerHandler`, its claim store and daily-review mark store (`FirestoreTaskClaimStore`,
`FirestoreDedupStore`, both on the worker-task dedup collection with production's TTLs), the note store
(`FirestoreAgentNoteAdapter`), the notification state store, `UserNotificationService`, `RemindersService`,
`AgentWorkerHandler`, `TaskDispatchService` and `GcpTaskQueue`, all on one `EvalDb`. What is scripted: the platform
channels (`PostingChannels`), the agent turn (`ScriptedRouter`, no LLM), the user repository, the agent factory, the
daily review's Gmail fetch (`ScriptedReview`) and the Cloud Tasks SDK client (`FailingTasksClient`, which refuses a used
task name as Cloud Tasks does and can fail a create before or after it landed).

The delivery helpers model what Cloud Tasks does with an answer (`worker_handler.py`, `_run_claimed`): a raise, a
status >= 500 or 429 releases the claim and the same task name is delivered again; anything else is acked.

Preconditions of a strict xfail (`xfail(strict=True, raises=AssertionError)`) are checked with `require(...)` and
`Twice.reraise()`, never with `assert`: a failed `assert` is the expected failure, so a broken setup would pass as XFAIL
before and after the fix. `require` raises `PreconditionFailed`, which is not an `AssertionError`, so the test reports
FAILED. Only the outcome under test is a bare `assert`.
"""
from __future__ import annotations

import asyncio
import functools
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import ModuleType
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Set, Tuple, Union
from unittest import mock

from google.api_core.exceptions import DeadlineExceeded

from src.adapters import firestore_agent_note_adapter
from src.adapters.dateutil_recurrence_adapter import DateutilRecurrenceAdapter
from src.adapters.firestore_agent_note_adapter import FirestoreAgentNoteAdapter
from src.adapters.firestore_dedup_store import FirestoreDedupStore
from src.adapters.firestore_notification_state_adapter import FirestoreNotificationStateAdapter
from src.adapters.firestore_task_claim_store import FirestoreTaskClaimStore
from src.adapters.gcp_task_queue import GcpTaskQueue
from src.config.environment import EnvironmentConfig
from src.domain.agent import AgentMessage, AgentResponse
from src.domain.agent_note import NoteCreate
from src.domain.cloud_task_delivery import CloudTaskDelivery
from src.domain.messaging import RichContent, SmartResponse
from src.domain.notification_kind import NotificationKind
from src.domain.user import UserBotConfig, UserProfile
from src.handlers.agent_worker_handler import AgentWorkerHandler
from src.handlers.worker_handler import WorkerHandler
from src.infrastructure.notification_sla import NOTIFICATION_SLA, dispatch_deadline_s
from src.ports.notification_channel_factory_port import NotificationChannelFactoryPort
from src.services.email_review_service import EmailReviewService
from src.services.reminders_service import RemindersService
from src.services.task_dispatch_service import TaskDispatchService
from src.services.user_notification_service import UserNotificationService
from tests.support.clock_instants import pin_datetime
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import SERVICE_URL, DedupCloudTasksClient
from tests.support.worker_payload_fields import consumers
from src.composition.connector_registry import ConnectorRegistry

# 09:00 in Berlin (CET, after the October transition). No boundary is under test at this instant.
T0 = datetime(2026, 10, 27, 8, 0, tzinfo=timezone.utc)
USER = "u1"
ACCOUNT = "a1"
# Not a `U…` id: `_send_answer_text` prefixes a mention to a Slack DM user id (user_notification_service.py:279-283).
CHANNEL = "C1"

# An answer is (body, status); a delivery that raised is the exception.
Answer = Tuple[dict, int]
Outcome = Union[Answer, BaseException]


# ----------------------------------------------------------------------------- preconditions

class PreconditionFailed(Exception):
    """A test's precondition does not hold. Deliberately not an `AssertionError`: under
    `xfail(strict=True, raises=AssertionError)` an `AssertionError` is the expected failure, this one reports FAILED."""


def require(cond: bool, what: str) -> None:
    """Check a precondition: raise `PreconditionFailed(what)` unless `cond`. A strict xfail checks every precondition
    with this (or `Twice.reraise()`), never with `assert`; an expected-pass test may use it for its setup checks too."""
    if not cond:
        raise PreconditionFailed(what)


# ----------------------------------------------------------------------------- the platform channels

@dataclass(frozen=True)
class Post:
    """One post a channel made: `kind` is "text" (`send_long_text`), "literal" (`send_literal`) or "rich"."""
    platform: str
    channel_id: str
    kind: str
    value: Any


class PostingChannel:
    """A platform channel as `UserNotificationService.notify` / `notify_raw` use it. Every post goes through the factory,
    which records it in order and may fail it (`PostingChannels.fail_post`).

    Not modelled: `send_document_link` (`notify_document_link`) and `send_message` (the media notices); no P15 card
    delivers an `agent_execution` body or a media notice. A test that needs them adds them here knowingly."""

    def __init__(self, factory: "PostingChannels", platform: str, channel_id: str) -> None:
        self._factory = factory
        self.platform = platform
        self.channel_id = channel_id

    async def send_long_text(self, text: str, link_list: Optional[List[Any]] = None) -> None:
        self._factory._post(Post(self.platform, self.channel_id, "text", text))

    async def send_rich_content(self, content: RichContent) -> None:
        self._factory._post(Post(self.platform, self.channel_id, "rich", content))

    async def send_literal(self, text: str, to_message_id: Optional[str] = None) -> None:
        self._factory._post(Post(self.platform, self.channel_id, "literal", text))


class PostingChannels(NotificationChannelFactoryPort):
    """One `PostingChannel` per (platform, channel_id); a platform in `unconfigured` has none (`create` answers None).

    `posts` holds every post that went through, in order, across channels and kinds; `texts` the text values.
    `fail_post(n)` makes the n-th post from now (1-based, counted across kinds and channels) raise `RuntimeError` once;
    that post is not recorded."""

    def __init__(self) -> None:
        self.channels: Dict[Tuple[str, str], PostingChannel] = {}
        self.unconfigured: Set[str] = set()
        self.posts: List[Post] = []
        self._attempts = 0
        self._failing: Set[int] = set()

    def create(self, platform: str, channel_id: str) -> Optional[PostingChannel]:  # type: ignore[override]
        if platform in self.unconfigured:
            return None
        return self.channels.setdefault((platform, channel_id), PostingChannel(self, platform, channel_id))

    def fail_post(self, n: int) -> None:
        if n < 1:
            raise ValueError("fail_post counts posts from 1")
        self._failing.add(self._attempts + n)

    def _post(self, post: Post) -> None:
        self._attempts += 1
        if self._attempts in self._failing:
            self._failing.discard(self._attempts)
            raise RuntimeError(f"post {self._attempts} failed ({post.kind})")
        self.posts.append(post)

    @property
    def texts(self) -> List[str]:
        return [p.value for p in self.posts if p.kind in ("text", "literal")]


# ----------------------------------------------------------------------------- the agent turn

@dataclass
class Turn:
    """One scripted agent turn: what the router answers, the ASYNC intents it enqueues first, or what it raises."""
    response: AgentResponse
    enqueue: Tuple[str, ...] = ()
    raises: Optional[BaseException] = None


def ok(text: str, rich: Optional[RichContent] = None, enqueue: Sequence[str] = ()) -> Turn:
    return Turn(AgentResponse.success(task_id="turn", agent_id="smart_response_agent",
                                      result=SmartResponse(text=text, structured_data=rich)), tuple(enqueue))


def failed(error: str = "provider down", enqueue: Sequence[str] = ()) -> Turn:
    return Turn(AgentResponse.failure(task_id="turn", agent_id="smart_response_agent", error=error), tuple(enqueue))


def timed_out(enqueue: Sequence[str] = ()) -> Turn:
    return Turn(AgentResponse.timeout(task_id="turn", agent_id="smart_response_agent", error="timed out"),
                tuple(enqueue))


class ScriptedRouter:
    """The coordinator as `UserNotificationService` and `AgentWorkerHandler` use it: `route_message` answers a scripted
    turn. A turn's `enqueue` intents are created through the world's real queue first, unnamed, as the coordinator's
    ASYNC path creates them (`agent_coordinator.py:558-564`), so a re-run turn repeats its dispatch as a real one would.
    `play` queues turns used one per call; `always` answers every call (after the queued ones)."""

    def __init__(self, queue: GcpTaskQueue) -> None:
        self._queue = queue
        self._turns: List[Turn] = []
        self._always: Optional[Turn] = None
        self.messages: List[AgentMessage] = []

    def play(self, *turns: Turn) -> None:
        self._turns.extend(turns)

    def always(self, turn: Turn) -> None:
        self._always = turn

    async def route_message(self, message: AgentMessage) -> AgentResponse:
        self.messages.append(message)
        if self._turns:
            turn = self._turns.pop(0)
        elif self._always is not None:
            turn = self._always
        else:
            # Not an AssertionError: a strict xfail would take it for its expected failure if it ever escaped.
            raise RuntimeError("no scripted turn")
        for intent in turn.enqueue:
            await self._queue.enqueue_agent_task(
                agent_id=f"{intent}_agent", intent=intent, query="q",
                context={"user_id": USER, "account_id": ACCOUNT},
            )
        if turn.raises is not None:
            raise turn.raises
        return turn.response


# ----------------------------------------------------------------------------- users, agents, the review fetch

class UserDirectory:
    """The user repository as the handler and `RemindersService` read it: `get_user` only."""

    def __init__(self) -> None:
        self._profiles: Dict[str, UserProfile] = {}
        self.put(UserProfile(user_id=USER, account_id=ACCOUNT, config=UserBotConfig(timezone="Europe/Berlin")))

    def put(self, profile: UserProfile) -> None:
        self._profiles[profile.user_id] = profile

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return self._profiles.get(user_id)


class AgentFactoryFake:
    """`ensure_agents_for_user` records the user ids it was asked for; nothing is built."""

    def __init__(self) -> None:
        self.ensured: List[str] = []

    async def ensure_agents_for_user(self, user_id: str) -> None:
        self.ensured.append(user_id)


class ScriptedReview(EmailReviewService):
    """The daily review with a scripted Gmail fetch: `fetch_review_payload` returns the given emails; `build_alert` is
    the real one."""

    def __init__(self, emails: List[dict]) -> None:
        super().__init__(email_provider=None, oauth_credentials=None,  # type: ignore[arg-type]
                         connector=ConnectorRegistry().get("gmail"))
        self.emails = list(emails)

    async def fetch_review_payload(self, user_id: str) -> Optional[List[dict]]:
        return list(self.emails)


# ----------------------------------------------------------------------------- the Cloud Tasks client

class FailingTasksClient(DedupCloudTasksClient):
    """`DedupCloudTasksClient` (a used name is refused) whose next creates can fail. `fail_next(times, landed, exc)`: each
    of the next `times` creates raises `exc` (default `DeadlineExceeded`), before recording the task (`landed=False`)
    or after the task was recorded (`landed=True`: the create landed and the answer was lost). `failed_attempts` counts
    the raises."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.failed_attempts = 0
        self._plan: List[Tuple[bool, Optional[BaseException]]] = []
        self._plan_guard = threading.Lock()   # creates run in worker threads (`GcpTaskQueue._create_task`)

    def fail_next(self, times: int = 1, landed: bool = False, exc: Optional[BaseException] = None) -> None:
        with self._plan_guard:
            self._plan.extend([(landed, exc)] * times)

    def create_task(self, request: Dict[str, Any], **kwargs: Any) -> Any:
        with self._plan_guard:
            step = self._plan.pop(0) if self._plan else None
            if step is not None:
                self.failed_attempts += 1
        if step is None:
            return super().create_task(request, **kwargs)
        landed, exc = step
        if landed:
            super().create_task(request, **kwargs)
        raise exc if exc is not None else DeadlineExceeded("create timed out")


def make_failing_queue() -> Tuple[GcpTaskQueue, FailingTasksClient]:
    """The real `GcpTaskQueue` over a `FailingTasksClient`, built as `recording_task_queue.make_task_queue` builds it."""
    with mock.patch("google.cloud.tasks_v2.CloudTasksClient", FailingTasksClient):
        queue = GcpTaskQueue("p", "us-central1", "q", SERVICE_URL)
    client = queue.client
    require(isinstance(client, FailingTasksClient), "the queue was built over FailingTasksClient")
    return queue, client


# ----------------------------------------------------------------------------- forcing a race

class Rendezvous:
    """Makes `parties` concurrent deliveries meet at a point the scheduler would not interleave by itself (a query read:
    `EvalDb` queries never yield).

    `arrive()` counts once per asyncio task (a task that already arrived passes straight through). The first arrivals
    set `reached` and wait until every party has arrived or ended (`done()`, called by `deliver_twice` in each delivery's
    `finally`), so a code path that never reaches the point cannot hang the test. `met` is True when the last party
    arrived while the others were still waiting at the point. `wrap(obj, name)` replaces that bound method on the
    instance with one that awaits the original, then `arrive()`: the race point is right after the read.

    The wait is bounded (`bound_s`, 2 s): when the other parties neither arrive nor end within it, `stalled` is set,
    `met` stays False and every arrival passes through from then on. That is the case of a fix that makes one delivery
    wait in-process on the other (a lock held across the race point): a precondition
    `require(rv.met and not rv.stalled, ...)` then reports FAILED instead of the test hanging. Written as
    `assert rv.met` in a strict xfail it would not: the failed assert is the expected failure, and the test stays XFAIL
    after the fix. A strict xfail whose named fix keeps the loser away from the point uses a fact the fix keeps (for
    example `rv.reached` with `overlapped`, not `met`).

    With a rendezvous, a pin's interleaving fact is `rv.met and not rv.stalled`: `Twice.overlapped` alone stays True
    during a stall (one delivery waits at the point while the other is blocked or takes another path).
    The bound is not a timing assertion; nothing asserts on elapsed time.

    One instance per race: `done()` marks are kept, so a reused instance does not wait again."""

    def __init__(self, parties: int = 2, bound_s: float = 2.0) -> None:
        self.parties = parties
        self.bound_s = bound_s
        self.reached = asyncio.Event()
        self.met = False
        self.stalled = False
        self._arrived: Set["asyncio.Task[Any]"] = set()
        self._ended: Set[Any] = set()
        self._waiting = 0
        self._change = asyncio.Event()

    @property
    def arrivals(self) -> int:
        """How many distinct tasks arrived."""
        return len(self._arrived)

    def _released(self) -> bool:
        return self.stalled or len(self._arrived | self._ended) >= self.parties

    def _poke(self) -> None:
        self._change.set()
        self._change = asyncio.Event()

    async def arrive(self) -> None:
        task = asyncio.current_task()
        if task is None or task in self._arrived:
            return
        self._arrived.add(task)
        self.reached.set()
        if len(self._arrived) >= self.parties and self._waiting == self.parties - 1:
            self.met = True
        self._poke()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.bound_s
        while not self._released():
            remaining = deadline - loop.time()
            if remaining <= 0:
                self.stalled = True
                self._poke()
                return
            self._waiting += 1
            try:
                await asyncio.wait_for(self._change.wait(), remaining)
            except asyncio.TimeoutError:
                pass
            finally:
                self._waiting -= 1

    def done(self) -> None:
        """The calling task's delivery ended (or will not run while the others are at the point)."""
        self._ended.add(asyncio.current_task())
        self._poke()

    def wrap(self, obj: Any, method_name: str) -> None:
        original = getattr(obj, method_name)

        @functools.wraps(original)
        async def at_the_point(*args: Any, **kwargs: Any) -> Any:
            result = await original(*args, **kwargs)
            await self.arrive()
            return result

        setattr(obj, method_name, at_the_point)


# ----------------------------------------------------------------------------- deliveries

async def deliver(handler: WorkerHandler, payload: dict, name: Optional[str] = None) -> Answer:
    """One `/worker` delivery: a Cloud Tasks delivery of task `name` (first attempt headers), or with no name a Scheduler
    body (production sends Scheduler bodies no Cloud Tasks headers, `cloud_task_delivery.py:26-30`)."""
    delivery = CloudTaskDelivery(name, 0, 0) if name else None
    return await handler.handle(payload, delivery=delivery, headers={})


def needs_retry(outcome: Outcome) -> bool:
    """Whether Cloud Tasks delivers the task again: the handler raised, or answered >= 500 or 429
    (`worker_handler.py:235-245`)."""
    if isinstance(outcome, BaseException):
        return True
    _, status = outcome
    return status >= 500 or status == 429


async def deliver_with_retry(handler: WorkerHandler, payload: dict, name: str, attempts: int = 2) -> List[Outcome]:
    """Deliver task `name`, and deliver it again under the same name while the outcome asks for a retry and attempts
    remain. The live agent queue allows 5 attempts (P15 arbiter log, read 2026-10-27); 2 are enough to show a repeat.
    An outcome is the `(body, status)` answer or the exception the delivery raised. A test that expects a raise checks its
    type (`require(isinstance(outcomes[0], DeadlineExceeded), ...)`), never just "an exception": a broken setup raises
    too."""
    outcomes: List[Outcome] = []
    for _ in range(attempts):
        try:
            outcome: Outcome = await deliver(handler, payload, name)
        except Exception as exc:
            outcome = exc
        outcomes.append(outcome)
        if not needs_retry(outcome):
            break
    return outcomes


@dataclass
class Twice:
    """Both deliveries' outcomes in delivery order, and whether both were inside `handle` at the same moment.

    A strict xfail starts its checks with `twice.reraise()` (or `require` on the exact exception type, when the raise is
    the behaviour under test), never `assert not twice.raised`: an answer that is an exception from a broken setup
    would otherwise fail an `assert` and pass as the expected failure, before and after the fix. `overlapped` stays True
    during a rendezvous stall; with a rendezvous the interleaving fact is `rv.met and not rv.stalled`."""
    answers: List[Outcome]
    overlapped: bool

    @property
    def raised(self) -> List[BaseException]:
        """The answers that are exceptions, in delivery order."""
        return [a for a in self.answers if isinstance(a, BaseException)]

    def reraise(self) -> None:
        """Re-raise the first answer that is an exception, so a strict xfail reports FAILED; no-op when no delivery
        raised. An `AssertionError` (the fakes raise one for an unsupported operation) is wrapped in
        `PreconditionFailed`: re-raised as itself it would pass as the expected failure."""
        if self.raised:
            first = self.raised[0]
            if isinstance(first, AssertionError):
                raise PreconditionFailed(f"a delivery raised AssertionError: {first}") from first
            raise first

    @property
    def statuses(self) -> List[Optional[int]]:
        """Each answer's HTTP status, in delivery order; None for an answer that is an exception."""
        return [None if isinstance(a, BaseException) else a[1] for a in self.answers]


async def deliver_twice(handler: WorkerHandler, payload: dict, *, gathered: bool,
                        names: Optional[Tuple[str, str]] = ("t-1", "t-2"),
                        rendezvous: Optional[Rendezvous] = None) -> Twice:
    """Two deliveries of one payload under names the claim does not merge (`names=None`: two Scheduler bodies). Each
    delivery runs as its own task, both under `asyncio.gather` when `gathered`, else one after the other; each calls
    `rendezvous.done()` when it ends. Run one after the other, the second is not running while the first is at the
    rendezvous point, so it is marked ended for the first (no wait). Exceptions are returned in `answers`."""
    inside = 0
    overlapped = False

    async def one(name: Optional[str]) -> Outcome:
        nonlocal inside, overlapped
        inside += 1
        if inside > 1:
            overlapped = True
        try:
            return await deliver(handler, payload, name)
        except Exception as exc:
            return exc
        finally:
            inside -= 1
            if rendezvous is not None:
                rendezvous.done()

    pair: Tuple[Optional[str], Optional[str]] = names if names is not None else (None, None)
    if gathered:
        answers = list(await asyncio.gather(one(pair[0]), one(pair[1])))
    else:
        if rendezvous is not None:
            rendezvous.done()
        answers = [await asyncio.create_task(one(pair[0])), await asyncio.create_task(one(pair[1]))]
    return Twice(answers=answers, overlapped=overlapped)


@functools.lru_cache(maxsize=None)
def _consumers() -> Dict[str, Tuple[FrozenSet[str], FrozenSet[str]]]:
    """Per task type, the payload keys `WorkerHandler` reads and requires (parses `worker_handler.py`: once per process)."""
    return {name: (c.read, c.required) for name, c in consumers().items()}


# ----------------------------------------------------------------------------- the world

class RedeliveryWorld:
    """Everything a redelivered `/worker` task touches, on one `EvalDb` (module docstring). `handler_overrides` are
    keywords of `WorkerHandler` that replace or add to the defaults (`email_review=`, `video_registry=`,
    `companion_extraction=`, `interactive_choice=`, ...)."""

    def __init__(self, **handler_overrides: Any) -> None:
        self.db = EvalDb()
        self.env = EnvironmentConfig()
        self.queue, self.client = make_failing_queue()
        self.dispatch = TaskDispatchService(self.queue)
        # As production builds them (application_graph.py:813-824): one collection, TTL 1 h and 48 h.
        self.claims = FirestoreTaskClaimStore(db_client=self.db, collection_name=self.env.worker_task_dedup_collection,
                                              ttl_seconds=3600)
        self.marks = FirestoreDedupStore(db_client=self.db, collection_name=self.env.worker_task_dedup_collection,
                                         ttl_seconds=172800)
        self.notes = FirestoreAgentNoteAdapter(self.db, self.env)
        self.state = FirestoreNotificationStateAdapter(self.db, self.env)
        self.channels = PostingChannels()
        self.router = ScriptedRouter(self.queue)
        self.notifier = UserNotificationService(self.state, self.channels, self.router, NOTIFICATION_SLA)
        self.users = UserDirectory()
        self.factory = AgentFactoryFake()
        self.reminders = RemindersService(self.notes, self.users, self.dispatch, DateutilRecurrenceAdapter(),
                                          dispatch_deadline_s=dispatch_deadline_s(NotificationKind.REMINDER))
        keywords: Dict[str, Any] = dict(
            agent_worker_handler=AgentWorkerHandler(self.router, notification_service=self.notifier,  # type: ignore[arg-type]
                                                    task_queue=self.dispatch),
            email_indexing_service=None, notification_service=self.notifier, consolidation_service=None,
            coordinator=self.router, agent_factory=self.factory, indexed_email_repo=None, user_repo=self.users,
            task_dispatch=self.dispatch, reminders_service=self.reminders, notes_port=self.notes,
            task_claims=self.claims, daily_review_marks=self.marks,
        )
        keywords.update(handler_overrides)
        self.handler = WorkerHandler(**keywords)

    async def set_primary_channel(self) -> None:
        await self.state.save_primary(USER, "slack", CHANNEL)

    @property
    def texts(self) -> List[str]:
        return self.channels.texts

    @property
    def posts(self) -> List[Post]:
        return self.channels.posts

    def bodies(self, task_type: Optional[str] = None) -> List[dict]:
        """The decoded bodies of every task created through the queue, in order; only `task_type`'s when given."""
        return [b for b in self.client.created_bodies() if task_type is None or b.get("task_type") == task_type]

    @property
    def claim_docs(self) -> List[dict]:
        """The task claims (documents of the dedup collection that carry an `attempt`; the daily-review marks share the
        collection and carry none), in id order."""
        docs = self.db.collection(self.env.worker_task_dedup_collection).docs
        return [dict(docs[k]) for k in sorted(docs) if "attempt" in docs[k]]

    @property
    def note_docs(self) -> Dict[str, dict]:
        """The stored notes by id."""
        return {k: dict(v) for k, v in self.db.collection(self.env.orchestrator_notes_collection).docs.items()}

    async def seed_note(self, monkeypatch: Any, *, due: datetime, recurrence: Optional[str] = None,
                        last_fired: Optional[datetime] = None, created_at: datetime) -> str:
        """Create a note for `u1` through the real adapter; its id is `created_at`'s millisecond, so each note needs its own
        `created_at`. `last_fired` (a claimed fire) is written on the stored document. Returns the note id."""
        module: ModuleType = firestore_agent_note_adapter
        pin_datetime(monkeypatch, module, created_at)
        note_id = str(int(created_at.timestamp() * 1000))
        if note_id in self.note_docs:
            raise ValueError(f"a note already has id {note_id}: give each note its own created_at")
        note = await self.notes.create_note(NoteCreate(
            user_id=USER, text="call dad", instruction="Remind the user to call dad.",
            due=due, recurrence=recurrence,
        ))
        if last_fired is not None:
            await self.db.collection(self.env.orchestrator_notes_collection).document(note.note_id).update(
                {"last_fired": last_fired})
        return note.note_id

    def payload(self, task_type: str, **fields: Any) -> dict:
        """`{"task_type": task_type, **fields}`, refused (`ValueError`, never an `AssertionError` a strict xfail would
        take for its own) unless `WorkerHandler` reads every given key for this type and every key it requires is
        given (`worker_payload_fields.consumers()`)."""
        table = _consumers()
        if task_type not in table:
            raise ValueError(f"WorkerHandler has no branch for task_type {task_type!r}")
        read, required = table[task_type]
        unread = sorted(set(fields) - set(read))
        missing = sorted(set(required) - set(fields))
        if unread or missing:
            raise ValueError(f"{task_type} payload: keys the handler does not read {unread}, required keys missing {missing}")
        return {"task_type": task_type, **fields}
