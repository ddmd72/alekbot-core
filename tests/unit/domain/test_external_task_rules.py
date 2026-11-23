"""The external task queue's transition rules (EXTERNAL_TASK_QUEUE_RFC §5.1, §5.2, §5.8), on task values alone.

Each rule is what a store transaction applies to the task it read; the races of §5.2 resolve because the loser re-runs
its rule on the winner's state. Here: the state each rule writes, and each precondition's refusal on the states that
fail it. The stores' application of the rules is `tests/unit/adapters/test_external_task_store_kit.py`; the service's
races are `tests/unit/services/test_external_task_service.py`.
"""
from datetime import timedelta

import pytest

from src.domain import external_task_rules as rules
from src.domain.external_task import (
    LEASE_S,
    MAX_CLAIMS_PER_TASK,
    MAX_RENEWALS_PER_TASK,
    RETENTION_S,
    DeliveryState,
    ExternalTask,
    ExternalTaskStatus,
    SubmitVia,
    TaskDelivery,
    TaskFailureCode,
    TaskHold,
    TaskLease,
    TaskOrigin,
    TaskResultMeta,
)
from src.domain.external_task_refusal import TaskRefusal
from tests.support.clock_instants import UTC_MIDNIGHT

T0 = UTC_MIDNIGHT.utc
S = ExternalTaskStatus
R = TaskRefusal
HOLD = TaskHold(code_digest="d" * 64, hold_until=T0 + timedelta(hours=1))


def task(**changes) -> ExternalTask:
    base = dict(task_id="tsk-1", user_id="usr-1", account_id="acc-1", worker_id="wrk-1", label="research",
                title="Irrigation kits", text="compare three community-garden irrigation kits for Shelbyville",
                origin=TaskOrigin(platform="slack", channel_id="C-1", session_id="usr-1:C-1"), status=S.QUEUED,
                created_at=T0, updated_at=T0, deadline_at=T0 + timedelta(days=7))
    base.update(changes)
    return ExternalTask(**base)


def leased(until_s: int = LEASE_S, **changes) -> ExternalTask:
    base = dict(status=S.LEASED, claims=1,
                lease=TaskLease(lease_id="lease-a", until=T0 + timedelta(seconds=until_s), generation=1))
    base.update(changes)
    return task(**base)


def meta(sha: str = "a" * 64) -> TaskResultMeta:
    return TaskResultMeta(bytes=3, sha256=sha, submitted_at=T0, submitted_via=SubmitVia.KEY)


NOW = T0 + timedelta(seconds=10)


def test_claim_leases_a_queued_task():
    out = rules.claim(task(), worker_id="wrk-1", expected_status=S.QUEUED, expected_generation=0,
                      lease_id="lease-a", now=NOW)
    assert out.written and out.task.status is S.LEASED and out.task.claims == 1
    assert out.task.lease == TaskLease(lease_id="lease-a", until=NOW + timedelta(seconds=LEASE_S), generation=1)
    assert out.task.updated_at == NOW and out.task.purge_after is None


def test_claim_takes_a_lapsed_lease_but_not_a_live_one():
    lapsed = leased(until_s=5)
    out = rules.claim(lapsed, worker_id="wrk-1", expected_status=S.LEASED, expected_generation=1,
                      lease_id="lease-b", now=NOW)
    assert out.written and out.task.lease.lease_id == "lease-b" and out.task.lease.generation == 2
    live = rules.claim(leased(), worker_id="wrk-1", expected_status=S.LEASED, expected_generation=1,
                       lease_id="lease-b", now=NOW)
    assert live.refusal is R.CONTENDED and not live.written


@pytest.mark.parametrize("expected_status, expected_generation", [(S.QUEUED, 1), (S.LEASED, 0)])
def test_claim_refuses_a_candidate_whose_status_or_generation_moved(expected_status, expected_generation):
    out = rules.claim(task(), worker_id="wrk-1", expected_status=expected_status,
                      expected_generation=expected_generation, lease_id="lease-a", now=NOW)
    assert out.refusal is R.CONTENDED and out.task == task()


def test_claim_expires_a_candidate_past_its_deadline():
    out = rules.claim(task(deadline_at=NOW), worker_id="wrk-1", expected_status=S.QUEUED, expected_generation=0,
                      lease_id="lease-a", now=NOW)
    assert out.written and out.task.status is S.EXPIRED and out.task.claims == 0
    assert out.task.purge_after == NOW + timedelta(seconds=RETENTION_S)


def test_claim_fails_a_candidate_at_the_claim_cap():
    out = rules.claim(task(claims=MAX_CLAIMS_PER_TASK), worker_id="wrk-1", expected_status=S.QUEUED,
                      expected_generation=0, lease_id="lease-a", now=NOW)
    assert out.written and out.task.status is S.FAILED and out.task.failure.code is TaskFailureCode.CLAIM_LOOP
    below = rules.claim(task(claims=MAX_CLAIMS_PER_TASK - 1), worker_id="wrk-1", expected_status=S.QUEUED,
                        expected_generation=0, lease_id="lease-a", now=NOW)
    assert below.task.status is S.LEASED and below.task.claims == MAX_CLAIMS_PER_TASK


@pytest.mark.parametrize("subject", [task(worker_id="wrk-2"), task(status=S.HELD, hold=HOLD)])
def test_a_worker_never_sees_another_workers_or_a_held_task(subject):
    assert rules.claim(subject, worker_id="wrk-1", expected_status=subject.status, expected_generation=0,
                       lease_id="lease-a", now=NOW).refusal is R.NOT_FOUND
    for out in (rules.renew(subject, worker_id="wrk-1", lease_id="lease-a", now=NOW),
                rules.submit(subject, worker_id="wrk-1", lease_id="lease-a", meta=meta(), now=NOW),
                rules.release(subject, worker_id="wrk-1", lease_id="lease-a", now=NOW),
                rules.fail(subject, worker_id="wrk-1", lease_id="lease-a", reason=None, now=NOW)):
        assert out.refusal is R.NOT_FOUND and not out.written


def test_renew_extends_a_live_lease_up_to_the_cap():
    out = rules.renew(leased(), worker_id="wrk-1", lease_id="lease-a", now=NOW)
    assert out.written and out.task.lease.until == NOW + timedelta(seconds=LEASE_S)
    assert out.task.lease.renewals == 1 and out.task.lease.generation == 1
    at_cap = leased(lease=TaskLease(lease_id="lease-a", until=T0 + timedelta(seconds=LEASE_S), generation=1,
                                    renewals=MAX_RENEWALS_PER_TASK))
    assert rules.renew(at_cap, worker_id="wrk-1", lease_id="lease-a", now=NOW).refusal is R.RENEW_CAP


def test_renew_refuses_a_lapsed_lease_and_another_lease():
    assert rules.renew(leased(until_s=10), worker_id="wrk-1", lease_id="lease-a", now=NOW).refusal is R.LEASE_EXPIRED
    assert rules.renew(leased(), worker_id="wrk-1", lease_id="lease-b", now=NOW).refusal is R.STALE_LEASE
    assert rules.renew(task(), worker_id="wrk-1", lease_id="lease-a", now=NOW).refusal is R.STALE_LEASE


def test_submit_accepts_a_live_or_lapsed_lease():
    for subject in (leased(), leased(until_s=5)):
        out = rules.submit(subject, worker_id="wrk-1", lease_id="lease-a", meta=meta(), now=NOW)
        assert out.written and out.task.status is S.DONE and out.task.result_meta == meta()
        assert out.task.purge_after == NOW + timedelta(seconds=RETENTION_S)


def test_submit_after_done_is_a_replay_with_the_same_body_and_refused_with_another():
    done = leased(status=S.DONE, result_meta=meta())
    replay = rules.submit(done, worker_id="wrk-1", lease_id="lease-a", meta=meta(), now=NOW)
    assert replay.ok and not replay.written and replay.task == done
    assert rules.submit(done, worker_id="wrk-1", lease_id="lease-a", meta=meta("b" * 64),
                        now=NOW).refusal is R.ALREADY_SUBMITTED


@pytest.mark.parametrize("subject", [
    task(),                                                        # re-queued by the lease timer
    leased(lease=TaskLease(lease_id="lease-b", until=T0 + timedelta(seconds=LEASE_S), generation=2)),  # re-claimed
    leased(status=S.CANCELLED),
    leased(status=S.FAILED),
    leased(status=S.EXPIRED),
])
def test_submit_on_anything_but_its_lease_is_stale(subject):
    out = rules.submit(subject, worker_id="wrk-1", lease_id="lease-a", meta=meta(), now=NOW)
    assert out.refusal is R.STALE_LEASE and not out.written


def test_release_and_fail_under_the_lease():
    released = rules.release(leased(), worker_id="wrk-1", lease_id="lease-a", now=NOW)
    assert released.task.status is S.QUEUED and released.task.lease == TaskLease(generation=2)
    assert released.task.claims == 1
    failed = rules.fail(leased(), worker_id="wrk-1", lease_id="lease-a", reason="no stock", now=NOW)
    assert failed.task.status is S.FAILED and failed.task.failure.code is TaskFailureCode.WORKER_FAILED
    assert failed.task.failure.reason == "no stock" and failed.task.purge_after is not None
    assert rules.release(leased(), worker_id="wrk-1", lease_id="lease-b", now=NOW).refusal is R.STALE_LEASE
    assert rules.fail(task(), worker_id="wrk-1", lease_id="lease-a", reason=None, now=NOW).refusal is R.STALE_LEASE


@pytest.mark.parametrize("status", [S.HELD, S.QUEUED, S.LEASED])
def test_cancel_ends_any_open_task(status):
    out = rules.cancel(task(status=status), now=NOW)
    assert out.written and out.task.status is S.CANCELLED and out.task.purge_after is not None


@pytest.mark.parametrize("status", [S.DONE, S.FAILED, S.CANCELLED, S.EXPIRED])
def test_cancel_after_a_final_state_is_refused(status):
    assert rules.cancel(task(status=status), now=NOW).refusal is R.ALREADY_FINAL


def test_confirm_queues_a_held_task_with_its_code_before_the_hold_runs_out():
    held = task(status=S.HELD, hold=HOLD)
    out = rules.confirm(held, code_digest="d" * 64, now=NOW)
    assert out.written and out.task.status is S.QUEUED and out.task.hold is None
    for wrong in (rules.confirm(held, code_digest="e" * 64, now=NOW),
                  rules.confirm(held, code_digest="d" * 64, now=HOLD.hold_until),
                  rules.confirm(task(), code_digest="d" * 64, now=NOW)):
        assert wrong.refusal is R.HOLD_NOT_FOUND and not wrong.written


def test_the_lease_timer_requeues_only_its_own_lapsed_lease():
    lapsed = leased(until_s=5)
    out = rules.expire_lease(lapsed, generation=1, now=NOW)
    assert out.written and out.task.status is S.QUEUED and out.task.lease == TaskLease(generation=2)
    for stale in (rules.expire_lease(lapsed, generation=0, now=NOW),          # an older lease's timer
                  rules.expire_lease(leased(), generation=1, now=NOW),        # renewed: until moved on
                  rules.expire_lease(task(status=S.DONE), generation=1, now=NOW)):
        assert stale.refusal is R.NOT_DUE and not stale.written


def test_the_deadline_timer():
    out = rules.expire_deadline(leased(), now=T0 + timedelta(days=7))
    assert out.written and out.task.status is S.EXPIRED
    assert rules.expire_deadline(task(), now=NOW).refusal is R.NOT_DUE
    assert rules.expire_deadline(task(status=S.DONE), now=T0 + timedelta(days=8)).refusal is R.ALREADY_FINAL


def test_the_hold_timer():
    held = task(status=S.HELD, hold=HOLD)
    assert rules.expire_hold(held, now=HOLD.hold_until).task.status is S.EXPIRED
    assert rules.expire_hold(held, now=NOW).refusal is R.NOT_DUE
    assert rules.expire_hold(task(), now=HOLD.hold_until).refusal is R.NOT_DUE


def test_delivery_fields_change_on_a_final_task_only_and_once():
    delivered = TaskDelivery(state=DeliveryState.DELIVERED, path="references/external_tasks/x.md")
    done = task(status=S.DONE)
    out = rules.record_delivery(done, delivery=delivered)
    assert out.written and out.task == done.model_copy(update={"delivery": delivered})
    again = rules.record_delivery(out.task, delivery=TaskDelivery(state=DeliveryState.DELIVERED, path="other.md"))
    assert again.ok and not again.written
    assert rules.record_delivery(task(), delivery=delivered).refusal is R.NOT_DUE
