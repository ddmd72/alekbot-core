"""The fan-out's cap alert (USER_DRIVE_INDEXING_RFC §5.4, §12.1; drive_cap_alert): files held by the card cap or the
chunk cap alert the ops sink once per cap when the cap starts holding files, with the count, and again only after the
cap held none in between. Real fan-out over the in-memory repository; the alert sink records what was posted."""
from datetime import timedelta
from typing import List

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState, Scope
from src.domain.drive_index_params import MAX_CARD_RECORDS_PER_USER, MAX_CHUNKS_PER_USER
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.support.drive_connection_service_fake import single_connection_repository
from tests.unit.services.drive_index_fakes import Clock, FakeQueue

USER = "user-a-0001"


class Alerts:
    def __init__(self) -> None:
        self.posts: List[str] = []

    async def post(self, text: str) -> None:
        self.posts.append(text)

    def caps(self) -> List[str]:
        return [p for p in self.posts if " cap:" in p]


@pytest.fixture
def env():
    clock = Clock()
    repo = InMemoryDriveIndexRepository()
    alerts = Alerts()

    svc = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), connections=single_connection_repository(USER),
                                    notifier=None, alert_sink=alerts, clock=clock)
    repo.states[USER] = DriveSyncState(user_id=USER, created_at=clock.now, last_success_at=clock.now)
    return svc, repo, alerts, clock


def _put(repo, item_id: str, **kw) -> None:
    repo.records[(USER, item_id)] = DriveIndexRecord(user_id=USER, item_id=item_id, name=f"{item_id}.epub",
                                                     kind="epub", parent_id="root", scope=Scope.NORMAL, **kw)


def _card_capped(repo, n: int) -> None:
    for i in range(n):
        _put(repo, f"book-{i}", state=RecordState.PENDING, card_capped=True)


def _chunk_capped(repo, n: int) -> None:
    for i in range(n):
        _put(repo, f"long-{i}", state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP)


def _clear(repo) -> None:
    repo.records.clear()


async def _run(svc, clock) -> None:
    await svc.fan_out()
    clock.advance(timedelta(minutes=10).total_seconds())


async def test_the_first_capped_files_alert_once_with_the_count(env):
    svc, repo, alerts, clock = env
    _card_capped(repo, 3)
    await _run(svc, clock)
    (post,) = alerts.caps()
    assert "card cap: 3 files" in post and str(MAX_CARD_RECORDS_PER_USER) in post
    assert USER[:8] in post and USER not in post   # the 8-character prefix, like every other fan-out alert
    assert repo.states[USER].card_cap_alerted and not repo.states[USER].chunk_cap_alerted


async def test_a_repeat_run_does_not_alert_again(env):
    svc, repo, alerts, clock = env
    _card_capped(repo, 3)
    await _run(svc, clock)
    _card_capped(repo, 4)   # more files held: still the same incident
    await _run(svc, clock)
    await _run(svc, clock)
    assert len(alerts.caps()) == 1 and repo.states[USER].card_cap_alerted


async def test_a_cap_that_cleared_alerts_again_when_it_holds_files_again(env):
    svc, repo, alerts, clock = env
    _chunk_capped(repo, 2)
    await _run(svc, clock)
    _clear(repo)
    await _run(svc, clock)
    assert len(alerts.caps()) == 1 and not repo.states[USER].chunk_cap_alerted   # cleared: lowered, nothing posted
    _chunk_capped(repo, 5)
    await _run(svc, clock)
    assert len(alerts.caps()) == 2 and "chunk cap: 5 files" in alerts.caps()[-1]
    assert repo.states[USER].chunk_cap_alerted


async def test_card_and_chunk_caps_are_counted_and_flagged_separately(env):
    svc, repo, alerts, clock = env
    _card_capped(repo, 2)
    _chunk_capped(repo, 5)
    _put(repo, "other", state=RecordState.METADATA_ONLY, reason=Reason.TOO_LARGE)   # not held by a cap
    await _run(svc, clock)
    card = [p for p in alerts.caps() if "card cap" in p]
    chunk = [p for p in alerts.caps() if "chunk cap" in p]
    assert len(card) == 1 and "card cap: 2 files" in card[0]
    assert len(chunk) == 1 and "chunk cap: 5 files" in chunk[0] and str(MAX_CHUNKS_PER_USER) in chunk[0]
    # the chunk cap clears while the card cap still holds: only the chunk flag comes down, nothing is posted
    for i in range(5):
        del repo.records[(USER, f"long-{i}")]
    await _run(svc, clock)
    assert len(alerts.caps()) == 2
    assert repo.states[USER].card_cap_alerted and not repo.states[USER].chunk_cap_alerted
    # a chunk cap incident after that alerts for the chunk cap alone
    _chunk_capped(repo, 1)
    await _run(svc, clock)
    assert len(alerts.caps()) == 3 and "chunk cap: 1 files" in alerts.caps()[-1]


async def test_nothing_capped_alerts_nothing(env):
    svc, repo, alerts, clock = env
    _put(repo, "book", state=RecordState.INDEXED, indexed_generation=1)
    summary = await svc.fan_out()
    assert alerts.caps() == [] and summary["alerts"] == 0
    assert not repo.states[USER].card_cap_alerted and not repo.states[USER].chunk_cap_alerted


async def test_the_alert_counts_in_the_fan_out_summary(env):
    svc, repo, alerts, clock = env
    _card_capped(repo, 1)
    _chunk_capped(repo, 1)
    summary = await svc.fan_out()
    assert summary["alerts"] == 2 and summary["errors"] == 0


async def test_without_an_alert_sink_no_flag_goes_up(env):
    svc, repo, alerts, clock = env
    unsinked = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), connections=svc._connections,
                                         notifier=None, alert_sink=None, clock=clock)
    _card_capped(repo, 2)
    await unsinked.fan_out()
    assert not repo.states[USER].card_cap_alerted   # a later run with a sink still alerts
    await svc.fan_out()
    assert len(alerts.caps()) == 1
