"""Month-scoped transcription reservations (USER_DRIVE_INDEXING_RFC §27.10, Δ106; plan G9): `reserve_model_usd` adds
to (or, negative, releases from) the RESERVATION's month with a server-side increment, in both adapters."""
import asyncio

import pytest

from src.adapters.firestore_drive_index_repo import usage_from_doc
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository

from tests.unit.adapters.firestore_drive_eval_fakes import USAGE, EvalDb, EvalDocRef, eval_transactions, make_eval_repo


@pytest.fixture
def repos(mock_env_config):
    db = EvalDb()
    with eval_transactions():
        yield db, [InMemoryDriveIndexRepository(), make_eval_repo(db, mock_env_config)]


async def test_reserve_and_release_same_month(repos):
    _, both = repos
    for repo in both:
        await repo.reserve_model_usd("u", "2026-10", "transcribe", 1.5)
        await repo.reserve_model_usd("u", "2026-10", "transcribe", 0.5)
        assert (await repo.get_model_usage("u", "2026-10")).transcribe_reserved_usd == 2.0
        await repo.reserve_model_usd("u", "2026-10", "transcribe", -2.0)
        assert (await repo.get_model_usage("u", "2026-10")).transcribe_reserved_usd == 0.0


async def test_release_after_month_turn_hits_reservation_month(repos):
    _, both = repos
    for repo in both:
        await repo.reserve_model_usd("u", "2026-10", "transcribe", 3.0)
        # collected on November 1st: the release names October, November is untouched
        await repo.reserve_model_usd("u", "2026-10", "transcribe", -3.0)
        assert (await repo.get_model_usage("u", "2026-10")).transcribe_reserved_usd == 0.0
        assert (await repo.get_model_usage("u", "2026-11")).transcribe_reserved_usd == 0.0


async def test_concurrent_reservations_never_lost(repos):
    _, both = repos
    for repo in both:
        await asyncio.gather(*(repo.reserve_model_usd("u", "2026-10", "transcribe", 0.25) for _ in range(40)))
        assert (await repo.get_model_usage("u", "2026-10")).transcribe_reserved_usd == pytest.approx(10.0)


async def test_firestore_writes_a_merge_increment_and_reads_the_field_back(repos, monkeypatch):
    from google.cloud.firestore_v1.transforms import Increment
    db, (_, fs) = repos
    written = []
    real_set = EvalDocRef.set

    async def capture(self, data, merge=False):
        written.append((data, merge))
        await real_set(self, data, merge)

    monkeypatch.setattr(EvalDocRef, "set", capture)
    await fs.reserve_model_usd("u", "2026-10", "transcribe", 1.25)
    payload, merge = written[0]
    assert merge is True and isinstance(payload["transcribe_reserved_usd"], Increment)
    doc = db.cols[USAGE].docs["u:2026-10"]
    assert doc["transcribe_reserved_usd"] == 1.25
    assert usage_from_doc(doc).transcribe_reserved_usd == 1.25


async def test_a_class_without_reservations_is_refused_before_any_write(repos):
    db, both = repos
    for repo in both:
        with pytest.raises(ValueError):
            await repo.reserve_model_usd("u", "2026-10", "profile", 1.0)
    assert USAGE not in db.cols or "u:2026-10" not in db.cols[USAGE].docs
