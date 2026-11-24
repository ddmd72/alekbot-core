"""ExternalTaskService (EXTERNAL_TASK_QUEUE_RFC §5.2, §5.3 contracts, §5.6, §5.7, §5.8 timers, §5.10, §6.5, §7).

The world: the service over the in-memory stores of `tests/support/external_task_stores.py` (the same rules the
Firestore adapter applies, its kit is `tests/unit/adapters/test_external_task_store_kit.py`), the real
`FirestoreUserRepository` on `EvalDb` (flag and lock written into the stored user, read fresh), the real
`InactiveUserCheck`, and the real `GcpTaskQueue` over a Cloud Tasks client that refuses a used task name. Assertions
are on what the stores hold, what readers return and the Cloud Tasks bodies the queue created.

Alex Doe is `usr-1` (account `acc-1`) with the worker `research`; `usr-2` is another user.
"""
import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Dict, List, Optional

import pytest

from src.domain.external_task_entry import TaskEntry
from src.adapters.firestore_account_repo import FirestoreAccountRepository
from src.adapters.firestore_user_repo import FirestoreUserRepository
from src.domain.external_task import (
    HOLD_CODE_DIGEST_PURPOSE,
    HOLD_S,
    LEASE_S,
    MAX_CLAIMS_PER_TASK,
    MAX_KEYS_PER_WORKER,
    MAX_OPEN_TASKS_PER_USER,
    MAX_RENEWALS_PER_TASK,
    MAX_WORKERS_PER_USER,
    RESULT_MAX_BYTES,
    TASK_MAX_AGE_S,
    TIMER_MARGIN_S,
    TITLE_MAX_CHARS,
    DeliveryState,
    ExternalTaskStatus,
    SubmitVia,
    TaskDelivery,
    TaskFailureCode,
    TaskOrigin,
    WorkerDestination,
)
from src.domain.external_task_jobs import (
    EXTERNAL_TASK_DELIVER,
    EXTERNAL_TASK_TIMER,
    DeliverPayload,
    TaskNotice,
    TimerKind,
    TimerPayload,
)
from src.domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from src.domain.external_worker_key import WORKER_KEY_DIGEST_PURPOSE, WORKER_KEY_PREFIX, has_key_shape
from src.domain.external_worker_principal import WorkerPrincipal
from src.domain.secret_digest import keyed_digest
from src.domain.user import InactiveReason, UserProfile
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.external_task_stores import FakeExternalTaskStore, FakeExternalWorkerStore
from tests.support.firestore_eval import EvalDb
from tests.support.recording_task_queue import make_task_queue

T0 = UTC_MIDNIGHT.utc
S = ExternalTaskStatus
R = TaskRefusal
DIGEST_KEY = bytes(range(32))
USERS = "users"
ORIGIN = TaskOrigin(platform="slack", channel_id="C-garden", session_id="usr-1:C-garden")
TITLE = "Irrigation kits"
TEXT = "compare three community-garden irrigation kits for Shelbyville"


class GatedTaskStore(FakeExternalTaskStore):
    """Makes the first two claim queries meet before either claims: `EvalDb`-like reads never yield on their own."""

    def __init__(self) -> None:
        super().__init__()
        self.gate: Optional[asyncio.Barrier] = None
        self.met = False

    async def queued_candidates(self, user_id: str, worker_id: str, limit: int):
        found = await super().queued_candidates(user_id, worker_id, limit)
        if self.gate is not None and self.gate.n_waiting < 2 and not self.met:
            gate = self.gate
            await asyncio.wait_for(gate.wait(), 2)
            self.met = True
        return found


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        env = SimpleNamespace(domain_users_collection=USERS, is_production=False)
        self.users = FirestoreUserRepository(self.db, env, FirestoreAccountRepository(self.db, "accounts"))
        self.tasks = GatedTaskStore()
        self.workers = FakeExternalWorkerStore()
        self.queue, self.client = make_task_queue()
        self.now = T0
        self._ids = 0
        self._bytes = 0
        self.service = ExternalTaskService(self.tasks, self.workers, self.queue, self.users,
                                           InactiveUserCheck(self.users), digest_key=DIGEST_KEY,
                                           clock=lambda: self.now, new_id=self._new_id,
                                           random_bytes=self._random_bytes)

    def _new_id(self) -> str:
        self._ids += 1
        return f"id-{self._ids}"

    def _random_bytes(self, n: int) -> bytes:
        self._bytes += 1
        return bytes([self._bytes % 256]) * n

    def at(self, seconds: float) -> None:
        self.now = T0 + timedelta(seconds=seconds)

    async def seed(self) -> "World":
        for uid, acc, email in (("usr-1", "acc-1", "alex@example.com"), ("usr-2", "acc-2", "other@example.com")):
            await self.users.create_user(UserProfile(user_id=uid, account_id=acc, email=email, display_name=uid))
            await self.flag(uid, True)
        self.research = await self.service.create_worker("usr-1", "research", "web research")
        self.watch = await self.service.create_worker("usr-1", "calendar-watch")
        self.minted = await self.service.mint_key("usr-1", self.research.worker_id)
        self.alex = await self.service.resolve_key(self.minted.key)
        self.other_worker = WorkerPrincipal("usr-1", "acc-1", self.watch.worker_id, SubmitVia.KEY)
        return self

    async def flag(self, user_id: str, on: bool) -> None:
        await self.db.collection(USERS).document(user_id).update({"config.external_tasks_enabled": on})

    async def lock(self, user_id: str) -> None:
        await self.db.collection(USERS).document(user_id).update(
            {"is_active": False, "inactive_reason": InactiveReason.DURESS.value})

    def jobs(self, task_type: str) -> List[Dict[str, Any]]:
        return [body for kind, body in self.client.worker_tasks() if kind == task_type]

    def schedule(self, index: int = -1) -> Optional[datetime]:
        stamp = self.client.create_task_requests[index]["task"].get("schedule_time")
        return stamp.ToDatetime(tzinfo=timezone.utc) if stamp is not None else None

    async def create(self, entry: TaskEntry = TaskEntry.REMINDER, label: str = "research", **kw: Any):
        return await self.service.create("usr-1", label, kw.get("title", TITLE), kw.get("text", TEXT), ORIGIN,
                                         entry=entry)

    async def claimed(self, principal: Optional[WorkerPrincipal] = None):
        created = await self.create()
        claim = await self.service.claim(principal or self.alex)
        assert claim is not None and claim.task_id == created.task_id
        return claim


@pytest.fixture
async def world() -> World:
    return await World().seed()


def delay_about(world: World, seconds: float, index: int = -1) -> bool:
    """The created task's schedule is `seconds` after the real enqueue instant (the adapter adds the delay to its own
    clock); a few seconds of slack for the run itself."""
    scheduled = world.schedule(index)
    return scheduled is not None and abs((scheduled - datetime.now(timezone.utc)).total_seconds() - seconds) < 5


async def refused(coro) -> TaskRefusal:
    with pytest.raises(ExternalTaskRefused) as caught:
        await coro
    return caught.value.reason


# ------------------------------------------------------------------------------------------------- create, confirm

async def test_an_untainted_create_queues_the_task_with_its_deadline_timer(world):
    world.at(5)
    created = await world.create(title="  Irrigation\n kits ", text=TEXT + "\r\n")
    assert created.status is S.QUEUED and created.code is None
    assert (created.label, created.title, created.text) == ("research", TITLE, TEXT)
    stored = world.tasks.tasks[created.task_id]
    assert (stored.user_id, stored.account_id, stored.worker_id) == ("usr-1", "acc-1", world.research.worker_id)
    assert stored.origin == ORIGIN and stored.hold is None and stored.claims == 0
    assert stored.deadline_at == world.now + timedelta(seconds=TASK_MAX_AGE_S)
    assert world.jobs(EXTERNAL_TASK_TIMER) == [{"task_type": EXTERNAL_TASK_TIMER, "user_id": "usr-1",
                                                "account_id": "acc-1", "task_id": created.task_id,
                                                "kind": "deadline", "generation": 0, "renewals": 0}]
    assert delay_about(world, TASK_MAX_AGE_S + TIMER_MARGIN_S + 1)


async def test_a_tainted_create_is_held_with_a_code_stored_as_its_digest_only(world):
    created = await world.create(entry=TaskEntry.CHAT)
    assert created.status is S.HELD and created.code is not None and len(created.code) == 8
    stored = world.tasks.tasks[created.task_id]
    assert stored.hold.code_digest == keyed_digest(DIGEST_KEY, HOLD_CODE_DIGEST_PURPOSE, created.code)
    assert stored.hold.hold_until == T0 + timedelta(seconds=HOLD_S)
    assert created.code not in stored.model_dump_json()
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold"]
    assert delay_about(world, HOLD_S + TIMER_MARGIN_S + 1)
    assert await world.service.claim(world.alex) is None
    assert await world.service.list_for_worker(world.alex) == []


async def test_a_chat_entry_waits_for_confirmation_and_a_reminder_entry_is_queued_at_once(world):
    chat = await world.create(entry=TaskEntry.CHAT)
    reminder = await world.create(entry=TaskEntry.REMINDER)
    assert (chat.status, reminder.status) == (S.HELD, S.QUEUED)
    assert chat.code is not None and reminder.code is None
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold", "deadline"]
    assert world.tasks.tasks[chat.task_id].hold is not None and world.tasks.tasks[reminder.task_id].hold is None
    assert "created_tainted" not in world.tasks.tasks[reminder.task_id].model_dump()
    claim = await world.service.claim(world.alex)
    assert claim is not None and claim.task_id == reminder.task_id     # the held task is offered to no worker


@pytest.mark.parametrize("label, title, text, reason", [
    ("export", TITLE, TEXT, R.UNKNOWN_WORKER),
    ("Research", TITLE, TEXT, R.UNKNOWN_WORKER),
    ("research", "t" * (TITLE_MAX_CHARS + 1), TEXT, R.TOO_LARGE),
    ("research", TITLE, " ​ ", R.INVALID_TEXT),
])
async def test_a_refused_create_stores_and_enqueues_nothing(world, label, title, text, reason):
    assert await refused(world.service.create("usr-1", label, title, text, ORIGIN, entry=TaskEntry.REMINDER)) is reason
    assert world.tasks.tasks == {} and world.jobs(EXTERNAL_TASK_TIMER) == []


async def test_a_revoked_workers_label_is_unknown_and_the_open_tasks_cap_holds(world):
    await world.service.revoke_worker("usr-1", world.watch.worker_id)
    assert await refused(world.create(label="calendar-watch")) is R.UNKNOWN_WORKER
    for _ in range(MAX_OPEN_TASKS_PER_USER):
        await world.create()
    assert await refused(world.create(entry=TaskEntry.CHAT)) is R.OPEN_TASKS_CAP
    await world.service.cancel("usr-1", next(iter(world.tasks.tasks)))
    assert (await world.create()).status is S.QUEUED


async def test_confirm_queues_the_held_task_once(world):
    created = await world.create(entry=TaskEntry.CHAT)
    world.at(60)
    confirmed = await world.service.confirm("usr-1", f"  {created.code.upper()} ")
    assert confirmed.task_id == created.task_id and confirmed.status is S.QUEUED
    assert world.tasks.tasks[created.task_id].hold is None
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold", "deadline"]
    assert await refused(world.service.confirm("usr-1", created.code)) is R.HOLD_NOT_FOUND
    assert (await world.service.claim(world.alex)).task_id == created.task_id


async def test_a_wrong_an_expired_or_a_foreign_code_changes_nothing(world):
    created = await world.create(entry=TaskEntry.CHAT)
    before = dict(world.tasks.tasks)
    assert await refused(world.service.confirm("usr-1", "00000000")) is R.HOLD_NOT_FOUND
    assert await refused(world.service.confirm("usr-1", "")) is R.HOLD_NOT_FOUND
    assert await refused(world.service.confirm("usr-2", created.code)) is R.HOLD_NOT_FOUND
    world.at(HOLD_S)
    assert await refused(world.service.confirm("usr-1", created.code)) is R.HOLD_NOT_FOUND
    assert world.tasks.tasks == before
    assert [b["kind"] for b in world.jobs(EXTERNAL_TASK_TIMER)] == ["hold"]


# ------------------------------------------------------------------------------------------------- owner reads

async def test_list_get_and_cancel_on_the_owner_side(world):
    first = await world.create()
    world.at(10)
    second = await world.create(label="calendar-watch", title="Term calendar",
                                text="find what changed in the Example School term calendar")
    listed = await world.service.list_tasks("usr-1")
    assert [t.task_id for t in listed] == [second.task_id, first.task_id]
    assert [t.task_id for t in await world.service.list_tasks("usr-1", worker_label="research")] == [first.task_id]
    claim = await world.service.claim(world.alex)
    assert [t.task_id for t in await world.service.list_tasks("usr-1", status=S.LEASED)] == [claim.task_id]
    await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B waters evenly.")
    view = await world.service.get_task("usr-1", first.task_id)
    assert view.summary.status is S.DONE and view.result == "Kit B waters evenly." and view.failure_reason is None
    queued = await world.service.get_task("usr-1", second.task_id)
    assert queued.result is None and queued.failure_reason is None
    assert (await world.service.cancel("usr-1", second.task_id)).status is S.CANCELLED
    assert await refused(world.service.cancel("usr-1", first.task_id)) is R.ALREADY_FINAL
    assert await refused(world.service.get_task("usr-1", "absent")) is R.NOT_FOUND


async def test_a_worker_failure_reason_comes_back_stripped_with_no_result_and_a_claim_loop_has_none(world):
    claim = await world.claimed()
    await world.service.fail(world.alex, claim.task_id, claim.lease_id, "supplier \x1b[31mdown")
    view = await world.service.get_task("usr-1", claim.task_id)
    assert view.failure_code is TaskFailureCode.WORKER_FAILED and view.failure_reason == "supplier [31mdown"
    assert view.result is None
    assert world.jobs(EXTERNAL_TASK_DELIVER) == [{"task_type": EXTERNAL_TASK_DELIVER, "user_id": "usr-1",
                                                  "account_id": "acc-1", "task_id": claim.task_id,
                                                  "notice": "failed"}]


# ------------------------------------------------------------------------------------------------- keys

async def test_a_minted_key_resolves_to_its_principal_and_is_stored_as_its_digest(world):
    assert has_key_shape(world.minted.key) and world.minted.key.startswith(WORKER_KEY_PREFIX)
    digest = keyed_digest(DIGEST_KEY, WORKER_KEY_DIGEST_PURPOSE, world.minted.key)
    assert set(world.workers.keys) == {digest} and world.minted.key_digest == digest
    record = world.workers.keys[digest]
    assert record.hint == world.minted.key[-4:] and world.minted.key not in record.model_dump_json()
    assert world.alex == WorkerPrincipal("usr-1", "acc-1", world.research.worker_id, SubmitVia.KEY)


async def test_a_malformed_key_costs_no_read_and_unknown_or_revoked_keys_are_one_refusal(world):
    reads = world.workers.get_key_reads
    for bad in ("", "Bearer x", WORKER_KEY_PREFIX + "A" * 42, WORKER_KEY_PREFIX + "A" * 42 + "=",
                "other_" + world.minted.key[len(WORKER_KEY_PREFIX):]):
        assert await refused(world.service.resolve_key(bad)) is R.KEY_INVALID
    assert world.workers.get_key_reads == reads
    assert await refused(world.service.resolve_key(WORKER_KEY_PREFIX + "B" * 43)) is R.KEY_INVALID
    assert world.workers.get_key_reads == reads + 1
    await world.service.revoke_key("usr-1", world.research.worker_id, world.minted.key_digest)
    assert await refused(world.service.resolve_key(world.minted.key)) is R.KEY_INVALID


async def test_a_key_is_refused_for_a_revoked_worker_a_locked_user_and_a_flag_off(world):
    await world.flag("usr-1", False)
    assert await refused(world.service.resolve_key(world.minted.key)) is R.NOT_ENABLED
    await world.flag("usr-1", True)
    await world.lock("usr-1")
    assert await refused(world.service.resolve_key(world.minted.key)) is R.ACCOUNT_LOCKED
    world2 = await World().seed()
    second = await world2.service.mint_key("usr-1", world2.watch.worker_id)
    await world2.service.revoke_worker("usr-1", world2.watch.worker_id)
    assert await refused(world2.service.resolve_key(second.key)) is R.KEY_INVALID      # its keys were revoked too
    world2.workers.keys[second.key_digest] = world2.workers.keys[second.key_digest].model_copy(
        update={"revoked_at": None})                                                 # a key the cleanup missed
    assert await refused(world2.service.resolve_key(second.key)) is R.WORKER_REVOKED


async def test_last_used_is_written_at_most_every_touch_interval(world):
    record = world.workers.keys[world.minted.key_digest]
    assert record.last_used_at == T0
    world.at(60)
    await world.service.resolve_key(world.minted.key)
    assert world.workers.keys[world.minted.key_digest].last_used_at == T0
    world.at(301)
    await world.service.resolve_key(world.minted.key)
    assert world.workers.keys[world.minted.key_digest].last_used_at == T0 + timedelta(seconds=301)


# ------------------------------------------------------------------------------------------------- claim

async def test_a_claim_takes_the_oldest_queued_task_and_enqueues_its_lease_timer(world):
    first = await world.create()
    world.at(1)
    await world.create()
    world.at(2)
    claim = await world.service.claim(world.alex)
    assert claim.task_id == first.task_id and (claim.title, claim.text) == (TITLE, TEXT)
    assert claim.lease_until == world.now + timedelta(seconds=LEASE_S) and claim.created_at == T0
    stored = world.tasks.tasks[first.task_id]
    assert stored.status is S.LEASED and stored.lease.lease_id == claim.lease_id and stored.claims == 1
    lease_timer = world.jobs(EXTERNAL_TASK_TIMER)[-1]
    assert TimerPayload.from_payload(lease_timer) == TimerPayload("usr-1", "acc-1", first.task_id, TimerKind.LEASE,
                                                                   generation=1, renewals=0)
    assert delay_about(world, LEASE_S + TIMER_MARGIN_S + 1)
    listed = await world.service.list_for_worker(world.alex)
    assert [(t.task_id, t.status, t.lease_until) for t in listed][0] == (first.task_id, S.LEASED, claim.lease_until)


async def test_no_task_is_none(world):
    assert await world.service.claim(world.alex) is None


async def test_two_concurrent_claims_of_one_task_one_wins(world):
    created = await world.create()
    world.tasks.gate = asyncio.Barrier(2)
    results = await asyncio.gather(world.service.claim(world.alex), world.service.claim(world.alex))
    assert world.tasks.met, "both claims read the candidate before either claimed it"
    assert sorted(r is not None for r in results) == [False, True]
    winner = next(r for r in results if r is not None)
    assert winner.task_id == created.task_id and world.tasks.tasks[created.task_id].claims == 1
    assert world.tasks.tasks[created.task_id].lease.lease_id == winner.lease_id


async def test_two_concurrent_claims_of_two_tasks_take_one_each(world):
    await world.create()
    world.at(1)
    await world.create()
    world.tasks.gate = asyncio.Barrier(2)
    results = await asyncio.gather(world.service.claim(world.alex), world.service.claim(world.alex))
    assert world.tasks.met
    assert len({r.task_id for r in results}) == 2


async def test_a_claim_expires_a_candidate_past_its_deadline_and_takes_the_next(world):
    stale = await world.create()
    world.at(TASK_MAX_AGE_S - 10)
    fresh = await world.create()
    world.at(TASK_MAX_AGE_S)
    claim = await world.service.claim(world.alex)
    assert claim.task_id == fresh.task_id
    assert world.tasks.tasks[stale.task_id].status is S.EXPIRED
    assert [DeliverPayload.from_payload(b) for b in world.jobs(EXTERNAL_TASK_DELIVER)] == [
        DeliverPayload("usr-1", "acc-1", stale.task_id, TaskNotice.EXPIRED)]


async def test_the_claim_after_the_cap_fails_the_task_with_claim_loop(world):
    created = await world.create()
    for n in range(MAX_CLAIMS_PER_TASK):
        claim = await world.service.claim(world.alex)
        await world.service.release(world.alex, claim.task_id, claim.lease_id)
    assert world.tasks.tasks[created.task_id].claims == MAX_CLAIMS_PER_TASK
    assert await world.service.claim(world.alex) is None
    stored = world.tasks.tasks[created.task_id]
    assert stored.status is S.FAILED and stored.failure.code is TaskFailureCode.CLAIM_LOOP
    assert [b["notice"] for b in world.jobs(EXTERNAL_TASK_DELIVER)] == ["failed"]


async def test_a_lapsed_lease_is_recovered_by_the_next_claim_and_the_old_holder_is_stale(world):
    first = await world.claimed()
    world.at(LEASE_S + 1)
    second = await world.service.claim(world.alex)
    assert second.task_id == first.task_id and second.lease_id != first.lease_id
    assert world.tasks.tasks[first.task_id].lease.generation == 2
    assert await refused(world.service.submit(world.alex, first.task_id, first.lease_id, "late")) is R.STALE_LEASE
    assert first.task_id not in world.tasks.results


# ------------------------------------------------------------------------------------------------- renew, submit

async def test_renew_extends_the_lease_until_the_cap(world):
    claim = await world.claimed()
    for n in range(1, MAX_RENEWALS_PER_TASK + 1):
        world.at(n * 60)
        grant = await world.service.renew(world.alex, claim.task_id, claim.lease_id)
        assert grant.lease_until == world.now + timedelta(seconds=LEASE_S)
    assert TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[-1]).renewals == MAX_RENEWALS_PER_TASK
    assert await refused(world.service.renew(world.alex, claim.task_id, claim.lease_id)) is R.RENEW_CAP


async def test_an_expired_lease_cannot_renew_but_can_still_submit_while_nobody_reclaimed_it(world):
    claim = await world.claimed()
    world.at(LEASE_S + 5)
    assert await refused(world.service.renew(world.alex, claim.task_id, claim.lease_id)) is R.LEASE_EXPIRED
    receipt = await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B waters evenly.")
    assert not receipt.replay and world.tasks.tasks[claim.task_id].status is S.DONE


async def test_submit_stores_the_result_and_enqueues_its_delivery(world):
    claim = await world.claimed()
    world.at(30)
    receipt = await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B\r\nwaters evenly.")
    stored = world.tasks.tasks[claim.task_id]
    assert stored.status is S.DONE and stored.result_meta.sha256 == receipt.sha256
    assert stored.result_meta.submitted_via is SubmitVia.KEY and stored.result_meta.bytes == receipt.bytes
    assert world.tasks.results[claim.task_id] == ("Kit B\nwaters evenly.", stored.purge_after)
    assert [DeliverPayload.from_payload(b) for b in world.jobs(EXTERNAL_TASK_DELIVER)] == [
        DeliverPayload("usr-1", "acc-1", claim.task_id, TaskNotice.DONE)]


async def test_a_submit_replay_changes_nothing_and_another_body_is_refused(world):
    claim = await world.claimed()
    first = await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B waters evenly.")
    before = (dict(world.tasks.tasks), dict(world.tasks.results))
    world.at(90)
    replay = await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B waters evenly.")
    assert replay.replay and replay.sha256 == first.sha256
    assert await refused(world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit C.")) \
        is R.ALREADY_SUBMITTED
    assert (dict(world.tasks.tasks), dict(world.tasks.results)) == before
    assert len(world.jobs(EXTERNAL_TASK_DELIVER)) == 1        # the replay's re-enqueue met the same task name


async def test_an_oversized_result_is_refused_and_the_lease_stays(world):
    claim = await world.claimed()
    assert await refused(world.service.submit(world.alex, claim.task_id, claim.lease_id,
                                              "x" * (RESULT_MAX_BYTES + 1))) is R.TOO_LARGE
    assert world.tasks.tasks[claim.task_id].status is S.LEASED
    assert (await world.service.submit(world.alex, claim.task_id, claim.lease_id, "short")).bytes == 5


async def test_cancel_then_submit_stores_nothing_and_submit_then_cancel_is_already_final(world):
    claim = await world.claimed()
    await world.service.cancel("usr-1", claim.task_id)
    assert await refused(world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B.")) is R.STALE_LEASE
    assert claim.task_id not in world.tasks.results and world.jobs(EXTERNAL_TASK_DELIVER) == []
    other = await world.claimed()
    await world.service.submit(world.alex, other.task_id, other.lease_id, "Kit B.")
    assert await refused(world.service.cancel("usr-1", other.task_id)) is R.ALREADY_FINAL
    assert world.tasks.tasks[other.task_id].status is S.DONE


async def test_release_requeues_and_counts_through_its_claim(world):
    claim = await world.claimed()
    await world.service.release(world.alex, claim.task_id, claim.lease_id)
    stored = world.tasks.tasks[claim.task_id]
    assert stored.status is S.QUEUED and stored.lease.lease_id is None and stored.claims == 1
    assert await refused(world.service.release(world.alex, claim.task_id, claim.lease_id)) is R.STALE_LEASE


# ------------------------------------------------------------------------------------------------- timers

async def test_the_lease_timer_requeues_a_lapsed_lease_and_a_stale_timer_is_a_noop(world):
    claim = await world.claimed()
    timer = TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[-1])
    world.at(60)
    await world.service.renew(world.alex, claim.task_id, claim.lease_id)
    world.at(LEASE_S + 30)                                        # the first lease's instant, but it was renewed
    assert await world.service.on_timer(timer) is False
    assert world.tasks.tasks[claim.task_id].status is S.LEASED
    world.at(60 + LEASE_S + 30)
    renewed_timer = TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[-1])
    assert await world.service.on_timer(renewed_timer) is True
    assert world.tasks.tasks[claim.task_id].status is S.QUEUED
    assert await world.service.on_timer(renewed_timer) is False      # redelivered


async def test_a_lease_timer_after_submit_is_a_noop(world):
    claim = await world.claimed()
    timer = TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[-1])
    await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B.")
    world.at(LEASE_S + 60)
    assert await world.service.on_timer(timer) is False
    assert world.tasks.tasks[claim.task_id].status is S.DONE


async def test_the_deadline_timer_expires_an_open_task_with_a_notice(world):
    created = await world.create()
    timer = TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[0])
    world.at(TASK_MAX_AGE_S + TIMER_MARGIN_S)
    assert await world.service.on_timer(timer) is True
    assert world.tasks.tasks[created.task_id].status is S.EXPIRED
    assert [b["notice"] for b in world.jobs(EXTERNAL_TASK_DELIVER)] == ["expired"]
    assert await world.service.on_timer(timer) is False


async def test_an_early_deadline_timer_re_enqueues_itself_under_a_new_name(world):
    created = await world.create()
    timer = TimerPayload.from_payload(world.jobs(EXTERNAL_TASK_TIMER)[0])
    world.at(TASK_MAX_AGE_S - 100)                     # before the deadline: as a timer capped at the schedule limit
    assert await world.service.on_timer(timer) is False
    assert world.tasks.tasks[created.task_id].status is S.QUEUED
    timers = world.jobs(EXTERNAL_TASK_TIMER)
    assert len(timers) == 2 and timers[1]["kind"] == "deadline"
    names = [r["task"]["name"] for r in world.client.create_task_requests]
    assert len(set(names)) == 2


async def test_a_deadline_timer_after_a_lost_notice_enqueues_it_again(world):
    created = await world.create()
    world.at(TASK_MAX_AGE_S + 1)
    world.tasks.tasks[created.task_id] = world.tasks.tasks[created.task_id].model_copy(
        update={"status": S.EXPIRED, "purge_after": world.now})          # expired, its notice never enqueued
    timer = TimerPayload("usr-1", "acc-1", created.task_id, TimerKind.DEADLINE)
    assert await world.service.on_timer(timer) is False
    assert [b["notice"] for b in world.jobs(EXTERNAL_TASK_DELIVER)] == ["expired"]


async def test_the_hold_timer_expires_an_unconfirmed_hold_silently(world):
    held = await world.create(entry=TaskEntry.CHAT)
    confirmed = await world.create(entry=TaskEntry.CHAT)
    await world.service.confirm("usr-1", confirmed.code)
    world.at(HOLD_S + TIMER_MARGIN_S)
    assert await world.service.on_timer(TimerPayload("usr-1", "acc-1", held.task_id, TimerKind.HOLD)) is True
    assert await world.service.on_timer(TimerPayload("usr-1", "acc-1", confirmed.task_id, TimerKind.HOLD)) is False
    assert world.tasks.tasks[held.task_id].status is S.EXPIRED
    assert world.tasks.tasks[confirmed.task_id].status is S.QUEUED
    assert world.jobs(EXTERNAL_TASK_DELIVER) == []


async def test_delivery_reads_a_final_task_and_records_once(world):
    claim = await world.claimed()
    assert await world.service.load_for_delivery("usr-1", claim.task_id) is None          # not final yet
    await world.service.submit(world.alex, claim.task_id, claim.lease_id, "Kit B.")
    load = await world.service.load_for_delivery("usr-1", claim.task_id)
    assert load.result == "Kit B." and load.task.status is S.DONE
    assert await world.service.load_for_delivery("usr-2", claim.task_id) is None
    delivered = TaskDelivery(state=DeliveryState.DELIVERED, destination=WorkerDestination.WORKSPACE,
                             path="references/external_tasks/x.md", delivered_at=world.now)
    assert await world.service.record_delivery("usr-1", claim.task_id, delivered) is True
    assert await world.service.record_delivery("usr-1", claim.task_id, delivered) is False


# ------------------------------------------------------------------------------------------------- tenancy, gates

async def test_another_users_task_is_unreachable_with_this_users_credentials(world):
    created = await world.create()
    assert await refused(world.service.get_task("usr-2", created.task_id)) is R.NOT_FOUND
    assert await refused(world.service.cancel("usr-2", created.task_id)) is R.NOT_FOUND
    assert await world.service.list_tasks("usr-2") == []
    other = await world.service.create_worker("usr-2", "research")
    key = await world.service.mint_key("usr-2", other.worker_id)
    stranger = await world.service.resolve_key(key.key)
    assert await world.service.claim(stranger) is None
    claim = await world.service.claim(world.alex)
    for op in (world.service.renew(stranger, claim.task_id, claim.lease_id),
               world.service.submit(stranger, claim.task_id, claim.lease_id, "x"),
               world.service.release(stranger, claim.task_id, claim.lease_id),
               world.service.fail(stranger, claim.task_id, claim.lease_id, None)):
        assert await refused(op) is R.NOT_FOUND
    assert world.tasks.tasks[created.task_id].status is S.LEASED


async def test_another_workers_task_of_the_same_user_is_as_invisible(world):
    created = await world.create()
    assert await world.service.claim(world.other_worker) is None
    assert await world.service.list_for_worker(world.other_worker) == []
    claim = await world.service.claim(world.alex)
    for op in (world.service.renew(world.other_worker, claim.task_id, claim.lease_id),
               world.service.submit(world.other_worker, claim.task_id, claim.lease_id, "x"),
               world.service.release(world.other_worker, claim.task_id, claim.lease_id),
               world.service.fail(world.other_worker, claim.task_id, claim.lease_id, None)):
        assert await refused(op) is R.NOT_FOUND
    assert world.tasks.tasks[created.task_id].lease.lease_id == claim.lease_id


async def test_a_principal_naming_a_foreign_or_revoked_worker_is_refused(world):
    forged = WorkerPrincipal("usr-2", "acc-2", world.research.worker_id, SubmitVia.KEY)
    assert await refused(world.service.claim(forged)) is R.WORKER_REVOKED
    wrong_account = WorkerPrincipal("usr-1", "acc-2", world.research.worker_id, SubmitVia.KEY)
    assert await refused(world.service.claim(wrong_account)) is R.WORKER_REVOKED


async def test_flag_off_refuses_every_operation_and_deletes_nothing(world):
    claim = await world.claimed()
    held = await world.create(entry=TaskEntry.CHAT)
    await world.flag("usr-1", False)
    before = dict(world.tasks.tasks)
    for op in (world.create(), world.service.confirm("usr-1", held.code), world.service.list_tasks("usr-1"),
               world.service.get_task("usr-1", claim.task_id), world.service.cancel("usr-1", claim.task_id),
               world.service.claim(world.alex), world.service.list_for_worker(world.alex),
               world.service.renew(world.alex, claim.task_id, claim.lease_id),
               world.service.submit(world.alex, claim.task_id, claim.lease_id, "x"),
               world.service.release(world.alex, claim.task_id, claim.lease_id),
               world.service.fail(world.alex, claim.task_id, claim.lease_id, None),
               world.service.create_worker("usr-1", "export"), world.service.list_workers("usr-1"),
               world.service.mint_key("usr-1", world.research.worker_id)):
        assert await refused(op) is R.NOT_ENABLED
    assert world.tasks.tasks == before
    world.at(TASK_MAX_AGE_S + TIMER_MARGIN_S)                     # timers still run: tasks expire
    assert await world.service.on_timer(TimerPayload("usr-1", "acc-1", claim.task_id, TimerKind.DEADLINE))


async def test_a_locked_user_is_refused_before_the_flag_is_read(world):
    claim = await world.claimed()
    await world.lock("usr-1")
    for op in (world.create(), world.service.list_tasks("usr-1"), world.service.claim(world.alex),
               world.service.submit(world.alex, claim.task_id, claim.lease_id, "x")):
        assert await refused(op) is R.ACCOUNT_LOCKED
    assert world.tasks.tasks[claim.task_id].status is S.LEASED


async def test_a_user_that_is_not_stored_has_no_flag(world):
    assert await refused(world.service.list_tasks("usr-ghost")) is R.NOT_ENABLED


# ------------------------------------------------------------------------------------------------- registry

async def test_worker_registry_checks(world):
    assert await refused(world.service.create_worker("usr-1", "Research")) is R.LABEL_INVALID
    assert await refused(world.service.create_worker("usr-1", "research")) is R.LABEL_TAKEN
    assert await refused(world.service.create_worker("usr-1", "export",
                                                     destination=WorkerDestination.DRIVE)) is R.DESTINATION_UNAVAILABLE
    for n in range(MAX_WORKERS_PER_USER - 2):
        await world.service.create_worker("usr-1", f"w{n}")
    assert await refused(world.service.create_worker("usr-1", "export")) is R.WORKERS_CAP
    labels = {w.label for w in await world.service.list_workers("usr-1")}
    assert len(labels) == MAX_WORKERS_PER_USER and {"research", "calendar-watch"} <= labels
    assert (await world.service.create_worker("usr-2", "research")).account_id == "acc-2"


async def test_keys_are_capped_listed_and_revoked_per_worker(world):
    for _ in range(MAX_KEYS_PER_WORKER - 1):
        await world.service.mint_key("usr-1", world.research.worker_id)
    assert await refused(world.service.mint_key("usr-1", world.research.worker_id)) is R.KEYS_CAP
    assert len(await world.service.list_keys("usr-1", world.research.worker_id)) == MAX_KEYS_PER_WORKER
    assert await refused(world.service.list_keys("usr-2", world.research.worker_id)) is R.NOT_FOUND
    assert await refused(world.service.mint_key("usr-2", world.research.worker_id)) is R.NOT_FOUND
    assert await refused(world.service.revoke_key("usr-2", world.research.worker_id,
                                                  world.minted.key_digest)) is R.NOT_FOUND
    await world.service.revoke_key("usr-1", world.research.worker_id, world.minted.key_digest)
    assert (await world.service.mint_key("usr-1", world.research.worker_id)).worker_id == world.research.worker_id


async def test_removing_a_worker_revokes_it_and_its_keys_and_cancels_its_open_tasks(world):
    done = await world.claimed()
    await world.service.submit(world.alex, done.task_id, done.lease_id, "Kit B.")
    claim = await world.claimed()
    queued = await world.create()
    held = await world.create(entry=TaskEntry.CHAT)
    other = await world.create(label="calendar-watch")
    assert await world.service.revoke_worker("usr-1", world.research.worker_id) == 3
    statuses = {tid: t.status for tid, t in world.tasks.tasks.items()}
    assert statuses == {claim.task_id: S.CANCELLED, queued.task_id: S.CANCELLED, held.task_id: S.CANCELLED,
                        done.task_id: S.DONE, other.task_id: S.QUEUED}
    assert all(k.is_revoked for k in world.workers.keys.values())
    assert await refused(world.service.claim(world.alex)) is R.WORKER_REVOKED
    assert await refused(world.service.mint_key("usr-1", world.research.worker_id)) is R.WORKER_REVOKED
    assert await world.service.revoke_worker("usr-1", world.research.worker_id) == 0       # safe to repeat
    assert await refused(world.service.revoke_worker("usr-2", world.research.worker_id)) is R.NOT_FOUND
    assert [w.label for w in await world.service.list_workers("usr-1")] == ["calendar-watch"]
    assert (await world.service.create_worker("usr-1", "research")).label == "research"
