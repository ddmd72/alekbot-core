"""EmailIndexingJobRepository kit (P11 · G3-7): the docstring of `src/ports/email_indexing_job_repository.py` checked
against `FirestoreEmailJobRepository` on the evaluating Firestore fake (H1), with the index refusal ON. Assertions are on
the raw stored documents and on what the readers return.

H1 reads return plain aware datetimes where the client returns `DatetimeWithNanoseconds`, so `_to_job`'s normalisation
never runs on a stored value written through the adapter: the kit reaches it with raw documents whose datetimes are
`DatetimeWithNanoseconds`, and compares the others as instants.

The watchdog overwrite (P11-G3-5) is pinned through the real service in
`tests/unit/services/test_email_indexing_service_store_pins.py`.

`_adapter(db)` is the one place the adapter is constructed.
"""
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from google.api_core.datetime_helpers import DatetimeWithNanoseconds
from google.api_core.exceptions import NotFound, ServiceUnavailable
from pydantic import ValidationError

from src.adapters.firestore_email_job_repo import FirestoreEmailJobRepository
from src.domain.email import IndexingJob
from tests.support.clock_instants import UTC_MIDNIGHT
from tests.support.firestore_eval import EvalDb

INDEXES_FILE = Path(__file__).resolve().parents[3] / "config" / "firestore.indexes.json"
JOBS = "eval_email_indexing_jobs_v1"
T = UTC_MIDNIGHT.utc
DATE_FIELDS = ("last_email_date", "backfill_until", "max_email_date", "min_email_date", "started_at", "updated_at",
               "completed_at")


def _adapter(db: EvalDb) -> FirestoreEmailJobRepository:
    return FirestoreEmailJobRepository(db, SimpleNamespace(email_indexing_jobs_collection=JOBS))


class Store:
    def __init__(self) -> None:
        self.db = EvalDb(indexes=INDEXES_FILE, groups={JOBS: "development_email_indexing_jobs_v1"})
        self.repo = _adapter(self.db)

    @property
    def docs(self) -> dict:
        return self.db.collection(JOBS).docs


@pytest.fixture
def store():
    return Store()


def job(job_id="j1", user="u1", provider="gmail", status="running", started=T, updated=None, **over) -> IndexingJob:
    fields = dict(job_id=job_id, user_id=user, account_id="a1", provider=provider, triggered_by="cabinet", status=status,
                  started_at=started, updated_at=updated or started)
    fields.update(over)
    return IndexingJob(**fields)


def raw(store, job_id="j1", **over) -> None:
    doc = job(job_id).model_dump()
    for field, value in over.items():
        if value is ...:
            doc.pop(field)
        else:
            doc[field] = value
    store.docs[job_id] = doc


def naive(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


# -------------------------------------------------------------------------------------- create / get

async def test_create_stores_every_field_under_the_job_id_and_get_returns_the_same_job(store):
    created = job(mode="backfill", next_page_token="page-2", emails_fetched=150, emails_stored=120, errors=[{"email_id": "m1", "stage": "embed", "error": "timeout"}],
                  backfill_until=T - timedelta(days=30), last_email_date=T - timedelta(days=1))
    await store.repo.create_job(created)
    doc = store.docs["j1"]
    assert (doc["user_id"], doc["provider"], doc["status"], doc["mode"], doc["next_page_token"], doc["emails_fetched"]) == (
        "u1", "gmail", "running", "backfill", "page-2", 150)
    assert doc["errors"] == [{"email_id": "m1", "stage": "embed", "error": "timeout"}]
    got = await store.repo.get_job("j1")
    # H1 returns aware datetimes (production returns naive): compare the instants
    assert got.model_copy(update={f: None for f in DATE_FIELDS}) == created.model_copy(update={f: None for f in DATE_FIELDS})
    assert [naive(getattr(got, f)) for f in ("started_at", "updated_at", "backfill_until", "last_email_date")] == [
        naive(getattr(created, f)) for f in ("started_at", "updated_at", "backfill_until", "last_email_date")]
    assert got.completed_at is None


async def test_a_second_create_of_one_job_id_replaces_the_document(store):
    await store.repo.create_job(job())
    await store.repo.create_job(job(status="completed", emails_stored=5))
    assert list(store.docs) == ["j1"] and store.docs["j1"]["status"] == "completed" and store.docs["j1"]["emails_stored"] == 5


async def test_get_of_a_missing_job_is_none(store):
    assert await store.repo.get_job("never-created") is None


async def test_a_stored_datetime_with_nanoseconds_reads_as_a_naive_plain_datetime_in_every_date_field(store):
    stamp = DatetimeWithNanoseconds(2026, 10, 1, 8, 15, 30, 125000, tzinfo=timezone.utc)
    raw(store, **{field: stamp for field in DATE_FIELDS})
    got = await store.repo.get_job("j1")
    for field in DATE_FIELDS:
        value = getattr(got, field)
        assert (type(value), value.tzinfo, value) == (datetime, None, datetime(2026, 10, 1, 8, 15, 30, 125000)), field


# ------------------------------------------------------------------------------- get_latest_job, list_jobs

async def test_get_latest_job_is_the_newest_of_this_user_and_provider(store):
    await store.repo.create_job(job("g-old", started=T - timedelta(hours=2)))
    await store.repo.create_job(job("g-new", started=T - timedelta(hours=1)))
    await store.repo.create_job(job("o-newer", provider="outlook", started=T + timedelta(hours=1)))
    await store.repo.create_job(job("other-user", user="u2", started=T + timedelta(hours=2)))
    assert (await store.repo.get_latest_job("u1", "gmail")).job_id == "g-new"
    assert (await store.repo.get_latest_job("u1", "outlook")).job_id == "o-newer"
    assert (await store.repo.get_latest_job("u2", "gmail")).job_id == "other-user"
    assert await store.repo.get_latest_job("u3", "gmail") is None
    assert await store.repo.get_latest_job("u1", "yahoo") is None


async def test_a_job_without_started_at_is_not_considered_the_latest(store):
    await store.repo.create_job(job("dated", started=T - timedelta(days=3)))
    raw(store, "undated", started_at=...)
    assert (await store.repo.get_latest_job("u1", "gmail")).job_id == "dated"
    del store.docs["dated"]
    assert await store.repo.get_latest_job("u1", "gmail") is None


async def test_list_jobs_is_newest_first_at_most_limit_and_only_this_users(store):
    for i in range(4):
        await store.repo.create_job(job(f"j{i}", started=T + timedelta(hours=i), provider="gmail" if i % 2 else "outlook"))
    await store.repo.create_job(job("other-user", user="u2", started=T + timedelta(hours=9)))
    assert [j.job_id for j in await store.repo.list_jobs("u1", limit=3)] == ["j3", "j2", "j1"]
    assert [j.job_id for j in await store.repo.list_jobs("u1")] == ["j3", "j2", "j1", "j0"]
    assert [j.job_id for j in await store.repo.list_jobs("u2")] == ["other-user"]
    assert await store.repo.list_jobs("u3") == []


# --------------------------------------------------------------------------------- get_stale_running_jobs

async def test_stale_running_jobs_are_the_running_ones_strictly_before_the_threshold_of_every_user(store):
    threshold = T - timedelta(hours=2)
    await store.repo.create_job(job("stale", updated=T - timedelta(hours=3)))
    await store.repo.create_job(job("stale-other-user", user="u2", updated=T - timedelta(hours=5)))
    await store.repo.create_job(job("at-the-threshold", updated=threshold))
    await store.repo.create_job(job("fresh", updated=T - timedelta(minutes=5)))
    await store.repo.create_job(job("done", status="completed", updated=T - timedelta(hours=3)))
    await store.repo.create_job(job("failed", status="failed", updated=T - timedelta(hours=3)))
    got = await store.repo.get_stale_running_jobs(threshold)
    assert sorted(j.job_id for j in got) == ["stale", "stale-other-user"]
    assert await store.repo.get_stale_running_jobs(T - timedelta(days=1)) == []


# ------------------------------------------------------------------------------------------- update_job

async def test_update_merges_the_given_fields_and_leaves_the_others(store):
    await store.repo.create_job(job(emails_fetched=10, mode="reindex"))
    await store.repo.update_job("j1", {"emails_fetched": 160, "emails_stored": 140, "next_page_token": "page-3",
                                       "updated_at": T + timedelta(minutes=2)})
    doc = store.docs["j1"]
    assert (doc["emails_fetched"], doc["emails_stored"], doc["next_page_token"], doc["updated_at"]) == (
        160, 140, "page-3", T + timedelta(minutes=2))
    assert (doc["mode"], doc["user_id"], doc["status"], doc["started_at"]) == ("reindex", "u1", "running", T)


async def test_a_datetime_subclass_in_an_update_is_stored_as_the_same_instant(store):
    """The adapter rewrites a `DatetimeWithNanoseconds` as a plain datetime before the write (the SDK fails on the
    subclass in some versions); the fake cannot show that failure, so the outcome is the stored instant."""
    await store.repo.create_job(job())
    stamp = DatetimeWithNanoseconds(2026, 10, 2, 7, 0, 0, 500000, tzinfo=timezone.utc)
    await store.repo.update_job("j1", {"completed_at": stamp, "status": "completed"})
    assert store.docs["j1"]["completed_at"] == datetime(2026, 10, 2, 7, 0, 0, 500000, tzinfo=timezone.utc)
    assert store.docs["j1"]["status"] == "completed"


async def test_an_update_of_a_missing_job_raises_not_found_and_creates_nothing(store):
    with pytest.raises(NotFound):
        await store.repo.update_job("never-created", {"status": "failed"})
    assert store.docs == {}


# ------------------------------------------------------------------------------------ invalid documents

async def test_a_stored_job_that_fails_validation_raises_from_every_reader(store):
    await store.repo.create_job(job("good", updated=T - timedelta(hours=3)))
    raw(store, "bad", triggered_by=..., updated_at=T - timedelta(hours=3))
    with pytest.raises(ValidationError):
        await store.repo.get_job("bad")
    with pytest.raises(ValidationError):
        await store.repo.get_stale_running_jobs(T)          # one unreadable job takes the whole listing down
    with pytest.raises(ValidationError):
        await store.repo.list_jobs("u1")


async def test_a_date_field_that_is_not_a_timestamp_raises_attribute_error(store):
    raw(store, started_at="2026-10-01T08:00:00")
    with pytest.raises(AttributeError):
        await store.repo.get_job("j1")


async def test_backend_errors_propagate(store):
    await store.repo.create_job(job())
    store.db.collection(JOBS).fail = ServiceUnavailable("down")
    with pytest.raises(ServiceUnavailable):
        await store.repo.get_latest_job("u1", "gmail")
    with pytest.raises(ServiceUnavailable):
        await store.repo.get_stale_running_jobs(T)
