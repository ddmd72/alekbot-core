"""What the memory inbox's three `/worker` runners (triage, final, sweep) are tested against.

The real inbox store (`tests/support/memory_inbox_store.py`) and the real memory-write lease adapter share one evaluating
database; the users and accounts are small stores answering real `UserProfile` / `BillingAccount` objects; the task queue
is a recorder that keeps Cloud Tasks' name semantics (a second task of one name is refused: `None`) and also keeps each
call's delay and deadline; the two passes are scripted fakes that record the request they were given and the lease they
ran under. Time is a settable clock.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Awaitable, Callable, Dict, List, Optional

from src.adapters.firestore_memory_write_lease import FirestoreMemoryWriteLease
from src.domain.billing import BillingAccount
from src.domain.language import LanguageCode
from src.domain.memory_inbox import (
    AnswerResult,
    Basis,
    CandidateDraft,
    CandidateFields,
    DraftClass,
    Relation,
    RelationKind,
    SubmissionDraft,
    SubmittingClient,
    TriageDecisions,
)
from src.domain.memory_inbox_runs import FinalOutcome, PassRequest, TriageOutcome
from src.domain.user import UserBotConfig, UserProfile
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.memory_inbox_store import inbox_store

UTC = timezone.utc
LEASES = "eval_memory_write_leases"
CLIENT = SubmittingClient(client_id="c1", client_name="Desk assistant")
DRAFT = DraftClass(directive=False, temporal_class="event", context_priority="medium", expiry={"kind": "none"})


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


class Users:
    """`accounts`: user id -> account id (None: a profile without an account); `zones`, `days`: per user."""

    def __init__(self, accounts: Dict[str, Optional[str]], zones: Optional[Dict[str, str]] = None,
                 days: Optional[Dict[str, int]] = None) -> None:
        self.accounts = accounts
        self.zones = zones or {}
        self.days = days or {}

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        if user_id not in self.accounts:
            return None
        config = UserBotConfig(timezone=self.zones.get(user_id, "UTC"),
                               memory_inbox_triage_interval_days=self.days.get(user_id))
        return UserProfile(user_id=user_id, account_id=self.accounts[user_id], config=config)


class Accounts:
    """One account per id, the given user its owner; `defaults`: account id -> the account's default config."""

    def __init__(self, owners: Dict[str, str], defaults: Optional[Dict[str, UserBotConfig]] = None) -> None:
        self.owners = owners
        self.defaults = defaults or {}

    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        owner = self.owners.get(account_id)
        if owner is None:
            return None
        return BillingAccount(account_id=account_id, iam_policy={owner: "owner"},
                              account_defaults=self.defaults.get(account_id))


class RecordingTaskQueue:
    """Every worker enqueue (`attempts`); the ones that created a task (`created`). A name used before answers None."""

    def __init__(self) -> None:
        self.attempts: List[Dict[str, Any]] = []
        self.created: List[Dict[str, Any]] = []
        self.failing = False
        self._names: set = set()

    async def enqueue_worker_task(self, task_type, payload, delay_seconds=0, deadline_seconds=None, dedup_key=None):
        call = dict(task_type=task_type, payload=payload, delay_seconds=delay_seconds,
                    deadline_seconds=deadline_seconds, dedup_key=dedup_key)
        self.attempts.append(call)
        if self.failing:
            raise RuntimeError("Cloud Tasks unavailable")
        name = f"{task_type}-{dedup_key}" if dedup_key else None
        if name is not None and name in self._names:
            return None
        if name is not None:
            self._names.add(name)
        self.created.append(call)
        return name or "unnamed"

    @property
    def keys(self) -> set:
        return {call["dedup_key"] for call in self.created}


class Notifier:
    """`notify_ui_notice` recorder: (message, fields) per post."""

    def __init__(self) -> None:
        self.posts: List[Any] = []
        self.raises = False

    async def notify_ui_notice(self, user_id: str, account_id: str, message: Any, **fields: str) -> bool:
        if self.raises:
            raise RuntimeError("channel down")
        self.posts.append((user_id, account_id, message, fields))
        return True


class Languages:
    def __init__(self, code: LanguageCode = LanguageCode.EN) -> None:
        self.code = code

    async def resolve_ui_language(self, user_id: str) -> LanguageCode:
        return self.code


class FakePass:
    """A scripted `TriagePass` or `FinalPass`: answers `outcome` (or raises `error`), records each request and the lease
    document it ran under, and runs `during` (an awaitable hook) inside the run."""

    def __init__(self, db: Any, outcome: Any, account_id: str = "a1") -> None:
        self._leases = db.collection(LEASES)
        self._account_id = account_id
        self.outcome = outcome
        self.error: Optional[Exception] = None
        self.during: Optional[Callable[[], Awaitable[None]]] = None
        self.requests: List[PassRequest] = []
        self.leases_seen: List[Optional[dict]] = []

    async def run(self, request: PassRequest) -> Any:
        self.requests.append(request)
        doc = self._leases.docs.get(self._account_id)
        self.leases_seen.append(dict(doc) if doc else None)
        if self.during is not None:
            await self.during()
        if self.error is not None:
            raise self.error
        return self.outcome


class ProfileStep:
    def __init__(self, db: Any, account_id: str = "a1") -> None:
        self._leases = db.collection(LEASES)
        self._account_id = account_id
        self.calls: List[tuple] = []
        self.leases_seen: List[Optional[dict]] = []
        self.error: Optional[Exception] = None

    async def run(self, account_id: str, deadline: datetime) -> None:
        self.calls.append((account_id, deadline))
        doc = self._leases.docs.get(self._account_id)
        self.leases_seen.append(dict(doc) if doc else None)
        if self.error is not None:
            raise self.error


def triage_outcome(status: str = "completed", candidates: int = 0, sources: tuple = ()) -> TriageOutcome:
    return TriageOutcome(status=status, submissions=5, candidates=candidates, discarded=1, left=0, passes=1,
                         sources=sources)  # type: ignore[arg-type]


def final_outcome(status: str = "completed", wrote: int = 0, candidates: int = 1) -> FinalOutcome:
    return FinalOutcome(status=status, candidates=candidates, wrote=wrote, gave_up=0)  # type: ignore[arg-type]


class Rig:
    """The store, the lease, the queue, the users and the clock, for user `u1` of account `a1`."""

    def __init__(self, now: datetime, zone: str = "Europe/Madrid", days: Optional[int] = None) -> None:
        self.inbox = inbox_store()
        self.repo = self.inbox.repo
        self.db = self.inbox.db
        self.lease = FirestoreMemoryWriteLease(self.db, SimpleNamespace(memory_write_leases_collection=LEASES))
        self.queue = RecordingTaskQueue()
        self.users = Users({"u1": "a1"}, zones={"u1": zone}, days={"u1": days} if days else None)
        self.accounts = Accounts({"a1": "u1"})
        self.owner_zones = OwnerTimezoneResolver(self.accounts, self.users)
        self.clock = Clock(now)

    @property
    def lease_doc(self) -> Optional[dict]:
        return self.db.collection(LEASES).docs.get("a1")

    async def hold_lease(self, until: datetime) -> None:
        """Another run holds the account's lease."""
        assert await self.lease.acquire("a1", "other-run", self.clock.now, until)

    async def received(self, text: str = "Maria likes tea", at: Optional[datetime] = None, user: str = "u1",
                       account: str = "a1") -> None:
        draft = SubmissionDraft(text, Basis.SAID, "", "")
        await self.repo.add_submissions(user, account, CLIENT, [draft], at or self.clock.now, "2026-01-01")

    async def candidate(self, text: str = "Clara booked the Coimbra weekend", at: Optional[datetime] = None) -> str:
        """One waiting candidate of u1, made by a triage write at `at`; its id."""
        at = at or self.clock.now
        known = set(self.inbox.docs)
        await self.received(text, at)
        [sid] = [i for i in self.inbox.docs if i not in known]
        before = set(self.inbox.docs)
        fields = CandidateFields(text=text, happens_when="March 6-8", said_when="", basis=Basis.SAID)
        decisions = TriageDecisions(candidates=(CandidateDraft(
            source_ids=(sid,), fields=fields, draft=DRAFT, relation=Relation(RelationKind.NEW, None, None)),))
        await self.repo.apply_triage("u1", "a1", [sid], decisions, "en", at)
        [cid] = [i for i in self.inbox.docs if i not in before]
        return cid

    async def confirmed(self, at: datetime, text: str = "Alex trains with Robin") -> str:
        """A candidate confirmed at `at` (the state's `final_due_at` becomes `at` + an hour)."""
        cid = await self.candidate(text, at=at)
        assert await self.repo.confirm(cid, "u1", None, at) is AnswerResult.DONE
        return cid


def hours(count: float) -> timedelta:
    return timedelta(hours=count)

