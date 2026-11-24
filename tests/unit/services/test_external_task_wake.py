"""ExternalTaskService's wake-up (EXTERNAL_TASK_QUEUE_RFC §5.1 wake fields, §5.6 targets, §5.8 wake-up, §7).

The world of `test_external_task_service.py` (the service over the in-memory stores, the real user repository and
lock check, the real `GcpTaskQueue` over a recording Cloud Tasks client), plus the test cipher (real AES-GCM with the
AAD bound) and a recording `OutboundWebhookPort` that answers what a test sets. Assertions are on the stored worker
record, the Cloud Tasks bodies, what the webhook was asked to send and what `on_wake` returns.

Alex Doe is `usr-1` (account `acc-1`) with the workers `research` and `calendar-watch`; Alex's wake host is
`hooks.example.com`; the bot's own host is `svc.example.org`.
"""
from typing import Any, List, Mapping, Optional, Tuple

import pytest

from src.domain.external_task import LEASE_S, TIMER_MARGIN_S, ExternalTaskStatus
from src.domain.external_task_jobs import (
    EXTERNAL_TASK_TIMER,
    EXTERNAL_TASK_WAKE,
    TimerPayload,
    WakeOutcome,
    WakePayload,
    TimerKind,
)
from src.domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from src.domain.wake_refusal import WakeRefusal, WakeTargetRefused
from src.domain.wake_target import WakeTarget, wake_aad
from src.ports.outbound_webhook_port import OutboundWebhookPort
from src.services.external_task_service import ExternalTaskService
from src.services.inactive_user_check import InactiveUserCheck
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.unit.services.test_external_task_service import DIGEST_KEY, ORIGIN, TEXT, TITLE, World as TaskWorld

S = ExternalTaskStatus
URL = "https://hooks.example.com/wake?token=s3cret-query"
HEADERS = (("Authorization", "Bearer s3cret-header"),)


class RecordingWebhook(OutboundWebhookPort):
    def __init__(self) -> None:
        self.sent: List[Tuple[WakeTarget, dict]] = []
        self.answer: Optional[WakeRefusal] = None

    async def post(self, target: WakeTarget, body: Mapping[str, Any]) -> Optional[WakeRefusal]:
        self.sent.append((target, dict(body)))
        return self.answer


class World(TaskWorld):
    def __init__(self, *, cipher: bool = True, webhook: bool = True) -> None:
        super().__init__()
        self.cipher = AesGcmTestCipher()
        self.webhook = RecordingWebhook()
        self.service = ExternalTaskService(
            self.tasks, self.workers, self.queue, self.users, InactiveUserCheck(self.users), digest_key=DIGEST_KEY,
            clock=lambda: self.now, new_id=self._new_id, random_bytes=self._random_bytes,
            cipher=self.cipher if cipher else None, webhook=self.webhook if webhook else None,
            own_hosts=("SVC.example.org",))

    async def target(self, worker_id: Optional[str] = None) -> None:
        await self.service.save_wake_target("usr-1", worker_id or self.research.worker_id, URL, HEADERS)

    def worker(self, worker_id: Optional[str] = None):
        return self.workers.workers[("usr-1", worker_id or self.research.worker_id)]

    def wakes(self) -> List[dict]:
        return self.jobs(EXTERNAL_TASK_WAKE)

    def payload(self, generation: int, worker_id: Optional[str] = None) -> WakePayload:
        return WakePayload("usr-1", "acc-1", worker_id or self.research.worker_id, generation)


@pytest.fixture
async def world() -> World:
    return await World().seed()


# ---------------------------------------------------------------------------------------------------- targets (§5.6)

async def test_a_target_is_stored_as_one_ciphertext_bound_to_the_record_and_only_its_host_in_clear(world):
    worker = await world.service.save_wake_target("usr-1", world.research.worker_id, f"  {URL} ", HEADERS)
    stored = world.worker()
    assert worker == stored
    assert stored.wake_host == "hooks.example.com" and stored.has_wake is True and stored.wake_generation == 0
    record = stored.model_dump_json()
    assert "s3cret-query" not in record and "s3cret-header" not in record and "/wake" not in record
    plain = await world.cipher.decrypt(stored.wake_secret, aad=f"external_workers/usr-1:{world.research.worker_id}")
    assert WakeTarget.from_json(plain) == WakeTarget(URL, HEADERS)
    with pytest.raises(Exception):
        await world.cipher.decrypt(stored.wake_secret, aad=wake_aad("usr-1", world.watch.worker_id))


async def test_saving_again_replaces_the_whole_target(world):
    await world.target()
    await world.service.save_wake_target("usr-1", world.research.worker_id, "https://relay.example.net/", ())
    plain = await world.cipher.decrypt(world.worker().wake_secret, aad=wake_aad("usr-1", world.research.worker_id))
    assert WakeTarget.from_json(plain) == WakeTarget("https://relay.example.net/", ())
    assert world.worker().wake_host == "relay.example.net"


@pytest.mark.parametrize("url, code", [
    ("http://hooks.example.com/wake", WakeRefusal.SCHEME),
    ("https://hooks.example.com:8443/wake", WakeRefusal.PORT),
    ("https://203.0.113.7/wake", WakeRefusal.NAME),
    ("https://metadata.google.internal/", WakeRefusal.INTERNAL_NAME),
    ("https://svc.example.org/worker", WakeRefusal.OWN_HOST),
    ("https://my-worker-abc.a.run.app/", WakeRefusal.OWN_HOST),
])
async def test_a_target_the_name_rules_refuse_raises_its_code_and_stores_nothing(world, url, code):
    with pytest.raises(WakeTargetRefused) as refused:
        await world.service.save_wake_target("usr-1", world.research.worker_id, url, ())
    assert refused.value.refusal is code
    assert world.worker().has_wake is False and world.worker().wake_secret is None and world.cipher.encrypts == 0


async def test_a_framing_header_is_refused_at_save_time(world):
    with pytest.raises(WakeTargetRefused) as refused:
        await world.service.save_wake_target("usr-1", world.research.worker_id, URL, (("Host", "elsewhere.example"),))
    assert refused.value.refusal is WakeRefusal.HEADER and world.worker().has_wake is False


async def test_save_time_runs_no_dns_and_sends_nothing(world):
    await world.target()
    assert world.webhook.sent == []


async def test_a_target_is_refused_for_a_foreign_revoked_or_unflagged_worker_and_without_a_cipher(world):
    with pytest.raises(ExternalTaskRefused) as foreign:
        await world.service.save_wake_target("usr-2", world.research.worker_id, URL, ())
    assert foreign.value.reason is TaskRefusal.NOT_FOUND
    await world.service.revoke_worker("usr-1", world.watch.worker_id)
    with pytest.raises(ExternalTaskRefused) as revoked:
        await world.service.save_wake_target("usr-1", world.watch.worker_id, URL, ())
    assert revoked.value.reason is TaskRefusal.WORKER_REVOKED
    with pytest.raises(ExternalTaskRefused) as cleared:
        await world.service.clear_wake_target("usr-1", world.watch.worker_id)
    assert cleared.value.reason is TaskRefusal.WORKER_REVOKED
    await world.flag("usr-1", False)
    with pytest.raises(ExternalTaskRefused) as off:
        await world.service.save_wake_target("usr-1", world.research.worker_id, URL, ())
    assert off.value.reason is TaskRefusal.NOT_ENABLED
    bare = await World(cipher=False).seed()
    with pytest.raises(ExternalTaskRefused) as unavailable:
        await bare.service.save_wake_target("usr-1", bare.research.worker_id, URL, ())
    assert unavailable.value.reason is TaskRefusal.WAKE_UNAVAILABLE


async def test_a_worker_revoked_between_the_check_and_the_write_stores_nothing(world, monkeypatch):
    async def gone(*args: Any, **kwargs: Any) -> bool:
        return False
    monkeypatch.setattr(world.workers, "set_wake", gone)
    with pytest.raises(ExternalTaskRefused) as refused:
        await world.target()
    assert refused.value.reason is TaskRefusal.WORKER_REVOKED


async def test_clearing_a_target_makes_the_worker_poll(world):
    await world.target()
    worker = await world.service.clear_wake_target("usr-1", world.research.worker_id)
    assert worker.has_wake is False and worker.wake_host is None and worker.wake_secret is None


# ---------------------------------------------------------------------------------------------------- when a ping is due

async def test_an_untainted_create_pings_a_worker_with_a_target_under_a_new_generation(world):
    await world.target()
    await world.create()
    assert world.worker().wake_generation == 1
    assert world.wakes() == [{"task_type": "external_task_wake", "user_id": "usr-1", "account_id": "acc-1",
                              "worker_id": world.research.worker_id, "generation": 1}]
    name = world.client.create_task_requests[-1]["task"]["name"]
    assert name.split("/")[-1].startswith("external_task_wake-")


async def test_the_ping_body_carries_no_task_content(world):
    await world.target()
    await world.create(title="Irrigation kits for Shelbyville")
    body = repr(world.wakes())
    assert TEXT not in body and "Irrigation" not in body and "research" not in body


async def test_a_second_task_while_the_first_is_queued_adds_no_ping(world):
    await world.target()
    await world.create()
    world.at(60)
    await world.create()
    assert len(world.wakes()) == 1 and world.worker().wake_generation == 1


async def test_a_worker_without_a_target_and_a_held_task_never_ping(world):
    await world.create()
    assert world.wakes() == [] and world.worker().wake_generation == 0
    await world.target()
    await world.create(tainted=True, label="research")
    assert world.wakes() == [] and world.worker().wake_generation == 0


async def test_no_ping_while_the_worker_holds_a_live_lease(world):
    await world.target()
    await world.claimed()
    pinged = len(world.wakes())
    world.at(30)
    await world.create()
    assert len(world.wakes()) == pinged


async def test_a_confirmed_held_task_pings(world):
    await world.target()
    held = await world.create(tainted=True)
    assert world.wakes() == []
    await world.service.confirm("usr-1", held.code)
    assert [b["generation"] for b in world.wakes()] == [1]


async def test_a_lease_that_runs_out_with_work_left_pings_again_under_a_new_generation(world):
    await world.target()
    claim = await world.claimed()
    world.at(10)
    await world.create()                     # queued behind the live lease: no ping
    assert [b["generation"] for b in world.wakes()] == [1]
    world.at(LEASE_S + TIMER_MARGIN_S + 1)
    timer = [b for b in world.jobs(EXTERNAL_TASK_TIMER) if b["kind"] == "lease"][-1]
    changed = await world.service.on_timer(TimerPayload.from_payload(timer))
    assert changed and world.tasks.tasks[claim.task_id].status is S.QUEUED
    assert [b["generation"] for b in world.wakes()] == [1, 2]


async def test_a_stale_lease_timer_sends_no_ping(world):
    await world.target()
    claim = await world.claimed()
    await world.service.on_timer(TimerPayload("usr-1", "acc-1", claim.task_id, TimerKind.LEASE, generation=99))
    assert [b["generation"] for b in world.wakes()] == [1]


async def test_a_failure_while_starting_a_ping_never_fails_the_create(world, monkeypatch):
    await world.target()

    async def broken(*args: Any) -> Optional[int]:
        raise RuntimeError("store down")
    monkeypatch.setattr(world.workers, "bump_wake_generation", broken)
    created = await world.create()
    assert world.tasks.tasks[created.task_id].status is S.QUEUED and world.wakes() == []


async def test_a_worker_whose_target_went_away_before_the_bump_is_not_pinged(world, monkeypatch):
    await world.target()

    async def none(*args: Any) -> Optional[int]:
        return None
    monkeypatch.setattr(world.workers, "bump_wake_generation", none)
    await world.create()
    assert world.wakes() == []


# ---------------------------------------------------------------------------------------------------- on_wake

async def test_a_due_wake_decrypts_the_target_and_posts_a_content_free_body(world):
    await world.target()
    await world.create()
    run = await world.service.on_wake(world.payload(1))
    assert run.outcome is WakeOutcome.SENT
    assert world.webhook.sent == [(WakeTarget(URL, HEADERS),
                                   {"event": "tasks_available", "worker": world.research.worker_id})]


async def test_a_stale_generation_another_account_or_a_cleared_target_sends_nothing(world):
    await world.target()
    await world.create()
    assert (await world.service.on_wake(world.payload(0))).outcome is WakeOutcome.SKIPPED
    other = WakePayload("usr-1", "acc-2", world.research.worker_id, 1)
    assert (await world.service.on_wake(other)).outcome is WakeOutcome.SKIPPED
    await world.service.clear_wake_target("usr-1", world.research.worker_id)
    assert (await world.service.on_wake(world.payload(1))).outcome is WakeOutcome.SKIPPED
    missing = WakePayload("usr-1", "acc-1", "wrk-gone", 1)
    assert (await world.service.on_wake(missing)).outcome is WakeOutcome.SKIPPED
    assert world.webhook.sent == []


async def test_no_queued_work_sends_nothing(world):
    await world.target()
    created = await world.create()
    await world.service.cancel("usr-1", created.task_id)
    assert (await world.service.on_wake(world.payload(1))).outcome is WakeOutcome.SKIPPED
    assert world.webhook.sent == []


async def test_a_retryable_answer_is_retry_and_a_final_one_is_refused_with_the_label_and_origin(world):
    await world.target()
    await world.create()
    world.webhook.answer = WakeRefusal.SERVER_ERROR
    retry = await world.service.on_wake(world.payload(1))
    assert retry.outcome is WakeOutcome.RETRY and retry.refusal is WakeRefusal.SERVER_ERROR
    world.webhook.answer = WakeRefusal.ADDRESS
    refused = await world.service.on_wake(world.payload(1))
    assert refused.outcome is WakeOutcome.REFUSED and refused.refusal is WakeRefusal.ADDRESS
    assert refused.label == "research" and refused.origin == ORIGIN


async def test_without_the_guard_wired_a_due_wake_sends_nothing(world):
    bare = await World(webhook=False).seed()
    bare.workers.workers[("usr-1", bare.research.worker_id)] = world.worker().model_copy(update={
        "worker_id": bare.research.worker_id, "has_wake": True, "wake_secret": "c2VhbGVk",
        "wake_host": "hooks.example.com", "wake_generation": 1})
    await bare.create()
    generation = bare.worker().wake_generation
    assert generation == 2      # the ping was started: only the send needs the guard
    assert (await bare.service.on_wake(bare.payload(generation))).outcome is WakeOutcome.SKIPPED


async def test_a_title_is_never_sent(world):
    await world.target()
    await world.service.create("usr-1", "research", TITLE, TEXT, ORIGIN, tainted=False)
    await world.service.on_wake(world.payload(1))
    assert TITLE not in repr(world.webhook.sent) and TEXT not in repr(world.webhook.sent)


async def test_the_enqueued_body_is_the_payload_the_runner_reads(world):
    await world.target()
    await world.create()
    assert WakePayload.from_payload(world.wakes()[-1]) == world.payload(1)
