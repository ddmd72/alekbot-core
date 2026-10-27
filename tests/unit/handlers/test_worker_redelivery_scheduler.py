"""The Cloud Scheduler `/worker` types, each run twice (P15 card E).

Production sends a Scheduler body no Cloud Tasks headers (`src/domain/cloud_task_delivery.py:26-30`), so the claim
never merges two runs: every run is `delivery=None` (`deliver_twice(names=None)`). The production graph is built
(`prod_graph`), seeded through the graph's own objects, and the assertions read the stored documents and the bodies of
the Cloud Tasks created (every `FakeCloudTasksClient` of the graph).

Per type (`tests/support/worker_triggers.py::scheduler_task_types`), where "run twice" is pinned:

| Type | Sequential | Gathered |
|---|---|---|
| `fire_due_reminders` | one-time: `test_a_one_time_fire_ticked_twice_enqueues_once` (here); recurrent: `tests/unit/services/test_reminders_due_instants.py::TestDailyNineAcrossTransitions::test_a_second_tick_at_the_same_now_enqueues_nothing` | one-time: `test_a_one_time_fire_ticked_twice_concurrently_enqueues_once` (here); recurrent: `tests/unit/services/test_reminders_due_instants.py::TestDailyNineAcrossTransitions::test_two_gathered_ticks_fire_once_and_one_loses_the_claim` |
| `start_daily_email_review` | `tests/unit/handlers/test_worker_handler_daily_review_fanout.py::test_three_ticks_inside_window_enqueue_once`, `::test_scheduler_recall_in_same_hour_does_not_enqueue_twice` | the mark store: `tests/unit/adapters/test_firestore_dedup_store_reclaim.py::test_concurrent_reclaims_of_one_expired_mark_yield_one_winner`; the named task: `tests/unit/handlers/test_worker_handler_daily_review_fanout.py::test_enqueue_passes_dedup_key_and_deadline` |
| `start_email_indexing` | `test_the_auto_index_ticked_twice_starts_one_job` (here, the control) | `test_the_auto_index_ticked_twice_concurrently_starts_one_job` (here, strict xfail P15-D6) |
| `email_indexing_watchdog` | `test_the_watchdog_run_twice_fails_a_stale_job_once` (here) | harmless: both runs read the stale job by query and write the same status (`src/services/email_indexing_service.py:256-272`) |
| `repair_email_embeddings` | `test_the_embedding_repair_run_twice_repairs_once` (here) | equal writes, cost only: both runs may embed the same email and write the same vectors (`src/services/email_embedding_repair_service.py:56-62`) |
| `sweep_consolidation` | revival bounded and counted: `tests/unit/adapters/test_consolidation_queue_failed_bounded.py::test_sweep_revives_failed_and_counts_the_revival`, `::test_revivals_survive_a_new_failure_and_stop_at_the_cap` | the consolidation lease: `tests/unit/services/test_consolidation_service_lease.py::test_second_concurrent_run_of_the_account_defers_with_a_named_bucket_task`, `::test_concurrent_losers_in_one_bucket_enqueue_one_task` |
| `sweep_companion_consolidation` | one unnamed task per stuck session per run, by design (`src/handlers/worker_handler.py:532-548`) | the race those tasks make: `tests/unit/handlers/test_worker_redelivery_chains.py` (P15-D5) |
| `fact_lifecycle_sweep` | one `fact_review` task over two runs in a local day: `tests/unit/services/test_fact_lifecycle_sweep_review.py::test_two_runs_on_one_local_day_make_one_task_and_both_report_the_review_requested`; one ops post: `tests/unit/services/test_fact_lifecycle_sweep.py::test_a_second_run_of_the_day_replaces_the_snapshot_keeps_the_tool_errors_and_posts_nothing_more`; a retry under the same task name: `tests/unit/handlers/test_worker_handler_fact_lifecycle_sweep.py::test_a_failed_delivery_releases_its_claim_so_the_retry_runs_and_a_good_run_keeps_it` | the run guard: `tests/unit/adapters/test_fact_lifecycle_run_guard_store.py::test_two_runs_racing_for_the_guard_get_one_winner`, `::test_runs_that_all_read_the_free_guard_before_any_commits_still_get_one_winner` |
| `renew_all_task_subscriptions` | one `renew_task_subscriptions` per user per run, by design (`src/handlers/worker_handler.py:873-884`) | two fan-outs at once race one user's renewal (unguarded read, PATCH/POST, save: `src/services/task_setup_service.py:174-207,290-318`): not tested; the sequential double renewal is pinned in `tests/unit/handlers/test_worker_redelivery_todo.py::test_a_renewal_delivered_twice_replaces_an_orphan_once` |
| `billing_daily_summary` | a second run posts a second ops summary: P15-D8, overstated (a manual double run only); not tested | - |
| `start_drive_sync` | one `drive_sync` tick per user per bucket: `tests/unit/services/test_drive_sync_scheduler_service.py::TestFanOut::test_second_fan_out_in_same_bucket_is_deduplicated`; one reconnect notice per breakage: `tests/unit/services/test_drive_sync_scheduler_service.py::TestReconnectNotice::test_reconnect_notice_once_per_breakage` | P15-D7, overstated; not tested |
"""
from datetime import timedelta
from typing import Any, List

import pytest

from src.domain.email import IndexedEmail, IndexingJob
from src.domain.user import UserBotConfig, UserProfile
from tests.support.cloud_tasks_fake import FakeCloudTasksClient
from tests.support.clock_instants import pin_datetime
from tests.support.oauth_credentials_store import creds
from tests.support.prod_env import EMBEDDING
from tests.support.prod_graph import prod_graph  # noqa: F401  (the fixture)
from tests.support.redelivery_world import T0, Rendezvous, deliver_twice, require

pytestmark = pytest.mark.slow


def _bodies(task_type: str) -> List[dict]:
    return [b for c in FakeCloudTasksClient.instances for b in c.created_bodies() if b.get("task_type") == task_type]


def _payload(task_type: str) -> dict:
    return {"task_type": task_type}


# ----------------------------------------------------------------------------- fire_due_reminders (one-time note)

async def _seed_one_time_note(graph: Any, monkeypatch: Any) -> str:
    """A one-time note for `u1` due `T0`, created a day earlier through the graph's own note store (its id is the
    creation millisecond, so the adapter's clock is pinned)."""
    from src.adapters import firestore_agent_note_adapter
    from src.domain.agent_note import NoteCreate
    pin_datetime(monkeypatch, firestore_agent_note_adapter, T0 - timedelta(days=1))
    note = await graph.app.worker_handler._notes_port.create_note(NoteCreate(
        user_id="u1", text="call dad", instruction="Remind the user to call dad.", due=T0))
    return note.note_id


def _notes(graph: Any) -> Any:
    return graph.app.worker_handler._notes_port._col


async def test_a_one_time_fire_ticked_twice_enqueues_once(prod_graph, monkeypatch):
    """A one-time note keeps its `due` after the claim (there is no next one), so the second tick lists it again and
    loses the claim on the re-claim guard (`last_fired >= due`, `src/adapters/firestore_agent_note_adapter.py:296-299`).
    Regression: the guard dropped, so the second tick enqueues a second `execute_reminder`."""
    from src.services import reminders_service
    note_id = await _seed_one_time_note(prod_graph, monkeypatch)
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("fire_due_reminders"),
                                gathered=False, names=None)

    assert not twice.raised
    assert twice.answers == [({"enqueued": 1, "claim_lost": 0, "skipped": 0}, 200),
                             ({"enqueued": 0, "claim_lost": 1, "skipped": 0}, 200)]
    assert _bodies("execute_reminder") == [{"task_type": "execute_reminder", "note_id": note_id, "user_id": "u1",
                                            "due_at": T0.isoformat()}]
    assert _notes(prod_graph).docs[note_id]["last_fired"] == T0 + timedelta(seconds=30)


async def test_a_one_time_fire_ticked_twice_concurrently_enqueues_once(prod_graph, monkeypatch):
    """Two ticks at once. Interleaving source: a `Rendezvous` right after `list_due_reminders` (a query, which never
    yields), so both ticks hold the note as due and unclaimed before either claims it; and `yield_on_get` on the notes
    collection, so the two claim transactions read the note before either commits (the claim's transactional read,
    `src/adapters/firestore_agent_note_adapter.py:280-302`). The loser's commit aborts and its re-run meets the re-claim
    guard (`:296-299`). Race evidence: `rv.met and not rv.stalled` (a serial run never meets at the point).
    Regression: the guard dropped, so the re-run claims too and two `execute_reminder` tasks are created."""
    from src.services import reminders_service
    note_id = await _seed_one_time_note(prod_graph, monkeypatch)
    pin_datetime(monkeypatch, reminders_service, T0 + timedelta(seconds=30))
    _notes(prod_graph).yield_on_get = True
    rv = Rendezvous()
    rv.wrap(prod_graph.app.worker_handler._reminders_service._notes_port, "list_due_reminders")

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("fire_due_reminders"),
                                gathered=True, names=None, rendezvous=rv)

    twice.reraise()
    require(rv.met and not rv.stalled, "both ticks listed the note before either claimed it")
    answers = sorted((body for body, _ in twice.answers), key=lambda b: b["enqueued"])
    assert answers == [{"enqueued": 0, "claim_lost": 1, "skipped": 0}, {"enqueued": 1, "claim_lost": 0, "skipped": 0}]
    assert twice.statuses == [200, 200]
    assert _bodies("execute_reminder") == [{"task_type": "execute_reminder", "note_id": note_id, "user_id": "u1",
                                            "due_at": T0.isoformat()}]
    assert _notes(prod_graph).docs[note_id]["last_fired"] == T0 + timedelta(seconds=30)


# ----------------------------------------------------------------------------- start_email_indexing

async def _auto_index_user(graph: Any) -> None:
    """`u1` with Gmail auto-indexing at 09:00 Berlin and Gmail credentials (the fan-out lists users by provider)."""
    await graph.app.user_repo.update_user(UserProfile(
        user_id="u1", account_id="a1",
        config=UserBotConfig(timezone="Europe/Berlin", gmail_auto_index=True, gmail_auto_index_hour=9)))
    await graph.app.worker_handler._email_indexing._oauth.save_credentials(creds("u1", "gmail"))


def _jobs(graph: Any) -> List[dict]:
    docs = graph.app.worker_handler._email_indexing._job_repo.collection.docs
    return [dict(d) for d in docs.values() if d.get("user_id") == "u1"]


async def test_the_auto_index_ticked_twice_starts_one_job(prod_graph, monkeypatch):
    """The control of P15-D6: run one after the other, the second tick finds the first job `running` (the running-job
    guard, `src/services/email_indexing_service.py:182-188`) and skips the user. `T0` is 09:00 in Berlin."""
    from src.handlers import worker_handler
    await _auto_index_user(prod_graph)
    pin_datetime(monkeypatch, worker_handler, T0)

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("start_email_indexing"),
                                gathered=False, names=None)

    assert not twice.raised
    assert twice.answers == [({"started": 1, "skipped": 0}, 200), ({"started": 0, "skipped": 1}, 200)]
    jobs = _jobs(prod_graph)
    assert [(j["status"], j["triggered_by"], j["mode"]) for j in jobs] == [("running", "scheduler", "incremental")]
    assert _bodies("email_indexing") == [{"task_type": "email_indexing", "job_id": jobs[0]["job_id"]}]


@pytest.mark.xfail(strict=True, raises=AssertionError,
                   reason="P15-D6: two concurrent auto-index ticks both pass the running-job guard and start two jobs; "
                          "owner decision")
async def test_the_auto_index_ticked_twice_concurrently_starts_one_job(prod_graph, monkeypatch):
    """Two ticks at once both read "no running job" and both create one. Interleaving source: a `Rendezvous` right
    after `get_latest_job` (a query, which never yields; `src/adapters/firestore_email_job_repo.py:74-87`), so both
    ticks pass the running-job guard (`src/services/email_indexing_service.py:182-188`) before either creates its job.
    Preconditions (hold today and after the fix): no delivery raised, and both ticks met at the point.
    Flipping fix: create the scheduler's job under a deterministic id per (user, provider, local date) with `create()`
    and skip the user on `AlreadyExists` (`email_indexing_service.py:199-206`; `firestore_email_job_repo.py:31`).
    Passing control: `test_the_auto_index_ticked_twice_starts_one_job`."""
    from src.handlers import worker_handler
    await _auto_index_user(prod_graph)
    pin_datetime(monkeypatch, worker_handler, T0)
    rv = Rendezvous()
    rv.wrap(prod_graph.app.worker_handler._email_indexing._job_repo, "get_latest_job")

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("start_email_indexing"),
                                gathered=True, names=None, rendezvous=rv)

    twice.reraise()
    require(twice.statuses == [200, 200], f"both ticks answered 200: {twice.answers}")
    require(rv.met and not rv.stalled, "both ticks read the latest job before either created one")
    assert len(_jobs(prod_graph)) == 1
    assert len(_bodies("email_indexing")) == 1


# ----------------------------------------------------------------------------- email_indexing_watchdog

async def test_the_watchdog_run_twice_fails_a_stale_job_once(prod_graph, monkeypatch):
    """The threshold is two hours before `T0` (`src/handlers/worker_handler.py:433-437`), and a stale job is one that is
    `running` and last updated before it (`get_stale_running_jobs`, `src/adapters/firestore_email_job_repo.py:101-109`).
    Of three jobs, only `j1` (running, updated 3 h before) is stale: `j2` is running but updated 1 h before, `j3` was
    updated 3 h before but is completed. The first run fails `j1`; the second finds nothing: `j1` is no longer running
    and was just updated (at `T0`). Regression: either filter dropped fails a job that is not stale.
    Gathered: both runs read the job by query and write the same status; harmless, not tested."""
    from src.handlers import worker_handler
    from src.services import email_indexing_service
    repo = prod_graph.app.worker_handler._email_indexing._job_repo
    for job_id, status, updated in (("j1", "running", 3), ("j2", "running", 1), ("j3", "completed", 3)):
        await repo.create_job(IndexingJob(job_id=job_id, user_id="u1", account_id="a1", provider="gmail",
                                          triggered_by="scheduler", status=status,
                                          started_at=T0 - timedelta(hours=4),
                                          updated_at=T0 - timedelta(hours=updated)))
    pin_datetime(monkeypatch, worker_handler, T0)
    pin_datetime(monkeypatch, email_indexing_service, T0)

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("email_indexing_watchdog"),
                                gathered=False, names=None)

    assert not twice.raised
    assert twice.answers == [({"status": "ok", "marked_failed": 1}, 200), ({"status": "ok", "marked_failed": 0}, 200)]
    assert {k: d["status"] for k, d in repo.collection.docs.items()} == {
        "j1": "failed", "j2": "running", "j3": "completed"}
    assert repo.collection.docs["j1"]["updated_at"] == T0


# ----------------------------------------------------------------------------- repair_email_embeddings

async def test_the_embedding_repair_run_twice_repairs_once(prod_graph):
    """One indexed email whose vectors are pending: the first run embeds it and clears the flag, the second finds
    nothing pending (`src/adapters/firestore_indexed_email_repo.py:336-360`). Neither run saturates the batch, so no
    follow-up `repair_email_embeddings` task is created. Gathered: both runs may embed the same email and write equal
    vectors; cost only, not tested."""
    repo = prod_graph.app.worker_handler._email_embedding_repair._email_repo
    await repo.save_batch([IndexedEmail(
        email_id="m1", user_id="u1", account_id="a1", source="gmail",
        text="The AirPods order ships on Friday.", tags=["order"], category="shopping",
        metadata={"subject": "Your order"}, subject="Your order", from_address="shop@example.com",
        email_date=T0 - timedelta(days=1), indexed_at=T0 - timedelta(hours=1), embedding_pending=True)])

    twice = await deliver_twice(prod_graph.app.worker_handler, _payload("repair_email_embeddings"),
                                gathered=False, names=None)

    assert not twice.raised
    assert twice.answers == [({"status": "ok", "repaired": 1, "has_more": False}, 200),
                             ({"status": "ok", "repaired": 0, "has_more": False}, 200)]
    stored = repo.collection.docs["u1_m1"]
    assert stored["embedding_pending"] is False
    assert {f: list(stored[f]) for f in ("vector", "tags_vector", "metadata_vector")} == {
        "vector": EMBEDDING, "tags_vector": EMBEDDING, "metadata_vector": EMBEDDING}
    assert _bodies("repair_email_embeddings") == []
