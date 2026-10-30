"""Drive eval runner: a record waiting for a later attempt (an image with no rendition yet, §24.5) is re-ticked at
its due_at, as production's 10-minute fan-out would, before the index counts as settled (round-1 test review B2)."""
from datetime import datetime, timedelta, timezone

from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from src.domain.connection_scope import ConnectionScope
from scripts.validation.drive_eval.eval_connection import EVAL_CONNECTION_ID
from scripts.validation.drive_eval.runner import EVAL_START, EVAL_USER, EvalClock, index_fixture
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import Reason

from ..services.drive_index_fakes import FakeEmbeddings
from .drive_eval_samples import MANIFEST, fake_content, rome

_HEIC = "Photos/Rome 2025/IMG_0001.HEIC"   # the fake triage has no rendition for it


async def test_an_image_without_a_rendition_is_retried_at_its_due_at_until_it_is_a_photograph():
    repo, clock = InMemoryDriveIndexRepository(), EvalClock()
    out = await index_fixture(FixtureDrive(MANIFEST), repo, FakeEmbeddings(), rome, "fake/8", clock,
                              throttle_pause_s=0, **fake_content())
    rec = repo.records[(EVAL_CONNECTION_ID, item_id_for(_HEIC))]
    assert rec.reason is Reason.PHOTOGRAPH and rec.due_at is None
    assert await repo.due_records(ConnectionScope(EVAL_USER, EVAL_CONNECTION_ID), datetime.max.replace(tzinfo=timezone.utc), 1) == []
    # the settled runner jumped the clock to each retry's due_at instead of stepping a minute a tick
    assert clock.now - EVAL_START > timedelta(seconds=60 * out.ticks)


async def test_a_waiting_record_never_lets_the_runner_report_settled_early():
    repo = InMemoryDriveIndexRepository()
    out = await index_fixture(FixtureDrive(MANIFEST), repo, FakeEmbeddings(), rome, "fake/8", EvalClock(),
                              throttle_pause_s=0, **fake_content())
    assert out.statuses.get("error", 0) == 0
    assert all(r.due_at is None for (conn, _), r in repo.records.items() if conn == EVAL_CONNECTION_ID)
