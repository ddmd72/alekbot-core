"""MemoryInboxService (MCP_MEMORY_SUBMISSIONS_RFC §5.6, §8.2, §10.3) over the real inbox store on the evaluating fake
(`tests/support/memory_inbox_store.py`) and the real Cloud Tasks queue adapter over a recording client
(`tests/support/recording_task_queue.py`). Assertions are on what the store holds and what the queue was asked to create."""
import hashlib
from datetime import datetime, timedelta, timezone
from typing import Optional

from src.domain.billing import BillingAccount
from src.domain.memory_inbox import (
    AddReceipt,
    AnswerResult,
    Answered,
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
from src.domain.memory_inbox_task_keys import final_key
from src.domain.user import UserBotConfig, UserProfile
from src.services.memory_inbox_service import MemoryInboxService
from src.services.owner_timezone_resolver import OwnerTimezoneResolver
from tests.support.memory_inbox_store import inbox_store
from tests.support.recording_task_queue import make_task_queue

UTC = timezone.utc
NOW = datetime(2026, 11, 25, 11, 30, tzinfo=UTC)
CLIENT = SubmittingClient(client_id="c1", client_name="Desk assistant")
TRIAGE_TEXT = "Clara booked the Coimbra weekend"
DRAFT = DraftClass(directive=False, temporal_class="event", context_priority="medium", expiry={"kind": "none"})


class Accounts:
    async def get_account(self, account_id: str) -> Optional[BillingAccount]:
        return BillingAccount(account_id=account_id, iam_policy={"u1": "owner"}) if account_id == "a1" else None


class Users:
    def __init__(self, zone: str) -> None:
        self.zone = zone

    async def get_user(self, user_id: str) -> Optional[UserProfile]:
        return UserProfile(user_id=user_id, account_id="a1", config=UserBotConfig(timezone=self.zone))


class FailingQueue:
    async def enqueue_worker_task(self, *args, **kwargs):
        raise RuntimeError("Cloud Tasks unavailable")


class World:
    def __init__(self, zone: str = "Pacific/Auckland", queue=None) -> None:
        self.inbox = inbox_store()
        self.queue, self.tasks = make_task_queue()
        self.now = NOW
        self.svc = MemoryInboxService(store=self.inbox.repo, task_queue=queue or self.queue,
                                      owner_zones=OwnerTimezoneResolver(Accounts(), Users(zone)), clock=lambda: self.now)

    async def candidate(self, text: str = TRIAGE_TEXT, at: datetime = NOW) -> str:
        """One waiting candidate of u1, made by a triage write at `at`; its id."""
        await self.inbox.repo.add_submissions("u1", "a1", CLIENT, [SubmissionDraft(text, Basis.SAID, "", "")], at,
                                              "2026-11-26")
        [sid] = [s.submission_id for s in await self.inbox.repo.received("u1", 40)]
        before = set(self.inbox.docs)
        fields = CandidateFields(text=text, happens_when="March 6-8", said_when="", basis=Basis.SAID)
        await self.inbox.repo.apply_triage("u1", "a1", [sid], TriageDecisions(candidates=(CandidateDraft(
            source_ids=(sid,), fields=fields, draft=DRAFT, relation=Relation(RelationKind.NEW, None, None)),)), "en", at)
        [cid] = [i for i in self.inbox.docs if i not in before]
        return cid


async def test_submit_counts_the_owners_local_day():               # Review Focus 4
    world = World(zone="Pacific/Auckland")

    receipt = await world.svc.submit(user_id="u1", account_id="a1", client=CLIENT,
                                     drafts=[SubmissionDraft("Alex trains with Robin", Basis.INFERRED, "", "")])

    assert receipt == AddReceipt(stored=1, over_day=0)
    assert (await world.inbox.repo.state("u1")).received_day == ("2026-11-26", 1)
    [doc] = world.inbox.docs.values()
    assert doc["received_at"] == NOW and doc["status"] == "received"


async def test_waiting_lists_candidates_newest_first_with_the_confirmed_count():
    world = World()
    older = await world.candidate("Maria likes tea", at=NOW - timedelta(days=2))
    confirmed = await world.candidate("Alex trains with Robin", at=NOW - timedelta(days=1))
    newer = await world.candidate(TRIAGE_TEXT, at=NOW)
    await world.svc.confirm("u1", confirmed, {})

    view = await world.svc.waiting("u1")

    assert [c.candidate_id for c in view.candidates] == [newer, older] and view.confirmed == 1
    assert (await world.svc.waiting("u2")).candidates == ()


async def test_confirm_enqueues_one_final_task_an_hour_later():
    world = World()
    cid = await world.candidate()
    before = datetime.now(UTC)

    answer = await world.svc.confirm("u1", cid, {"text": "Clara booked the Coimbra weekend, March 6-8"})

    after = datetime.now(UTC)
    assert answer == Answered(AnswerResult.DONE)
    doc = world.inbox.docs[cid]
    assert doc["text"] == "Clara booked the Coimbra weekend, March 6-8" and doc["edited"] is True
    assert doc["happens_when"] == "March 6-8" and doc["proposed"]["text"] == TRIAGE_TEXT
    assert world.tasks.worker_tasks() == [("memory_inbox_final",
                                           {"task_type": "memory_inbox_final", "user_id": "u1"})]
    [request] = [r["task"] for r in world.tasks.create_task_requests]
    assert request["dispatch_deadline"].seconds == 1800
    scheduled = request["schedule_time"].ToDatetime(tzinfo=UTC)      # the adapter anchors the delay at its wall clock
    assert before + timedelta(seconds=3600) <= scheduled + timedelta(microseconds=1)
    assert scheduled <= after + timedelta(seconds=3600)
    digest = hashlib.sha256(final_key("u1", NOW + timedelta(hours=1)).encode()).hexdigest()[:32]
    assert request["name"].endswith(f"/tasks/memory_inbox_final-{digest}")
    assert (await world.inbox.repo.state("u1")).final_due_at == NOW + timedelta(hours=1)


async def test_confirm_refuses_bad_fields_without_writing():
    world = World()
    cid = await world.candidate()
    stored = dict(world.inbox.docs[cid])

    answers = [await world.svc.confirm("u1", cid, edits) for edits in (
        {"text": " ".join(["word"] * 61)}, {"text": "  "}, {"basis": "heard"}, {"said_when": 2026})]

    assert answers == [Answered(AnswerResult.INVALID, "too_long"), Answered(AnswerResult.INVALID, "empty_text"),
                       Answered(AnswerResult.INVALID, "basis"), Answered(AnswerResult.INVALID, "said_when")]
    assert world.inbox.docs[cid] == stored and world.tasks.create_task_requests == []
    assert (await world.inbox.repo.state("u1")).final_due_at is None


async def test_confirm_takes_a_basis_edit_and_passes_the_stores_refusals_through():
    world = World()
    cid = await world.candidate()

    assert await world.svc.confirm("u1", cid, {"basis": "document", "said_when": None}) == Answered(AnswerResult.DONE)
    assert world.inbox.docs[cid]["basis"] == "document" and world.inbox.docs[cid]["said_when"] == ""
    assert await world.svc.confirm("u1", cid, {}) == Answered(AnswerResult.NOT_WAITING)
    assert await world.svc.confirm("u1", "missing", {"text": "Maria likes tea"}) == Answered(AnswerResult.NOT_FOUND)
    assert len(world.tasks.create_task_requests) == 1


async def test_a_failed_enqueue_still_answers_done():
    world = World(queue=FailingQueue())
    cid = await world.candidate()

    assert await world.svc.confirm("u1", cid, {}) == Answered(AnswerResult.DONE)

    assert world.inbox.docs[cid]["status"] == "confirmed" and world.inbox.docs[cid]["edited"] is False
    assert (await world.inbox.repo.state("u1")).final_due_at == NOW + timedelta(hours=1)


async def test_reject_enqueues_nothing():
    world = World()
    cid = await world.candidate()

    assert await world.svc.reject("u1", cid) == Answered(AnswerResult.DONE)
    assert await world.svc.reject("u2", cid) == Answered(AnswerResult.OTHER_USER)

    assert world.inbox.docs[cid]["status"] == "rejected" and world.inbox.docs[cid]["rejected_at"] == NOW
    assert world.tasks.create_task_requests == [] and (await world.inbox.repo.state("u1")).final_due_at is None
