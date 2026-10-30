"""The notice about files the bot cannot open backs off when it cannot be delivered (USER_DRIVE_INDEXING_RFC §26.2,
review of S1): no channel, or no file upload for the user's platform. The fan-out then does not read the marked records
again until the next UTC day, unless the tick counts more files waiting to be told about."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER
from src.domain.ui_messages import UIMessage
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService
from tests.unit.services.drive_index_fakes import T0, Clock, FakeDrive, FakeQueue, FakeUsers

MARKED = T0 + timedelta(minutes=1)


class Notifier:
    def __init__(self) -> None:
        self.file_sent = False
        self.files = 0

    async def notify_file_bytes(self, *a, **kw) -> bool:
        self.files += 1
        return self.file_sent

    async def notify_raw(self, *a, **kw) -> bool:
        return True


class Localization:
    def get_ui_string(self, lang, message):
        return "{count} closed" if message is UIMessage.DRIVE_INACCESSIBLE_NOTICE else "Closed"


def closed(item_id):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=f"{item_id}.pdf", parent_id="root", kind="pdf",
                            scope=Scope.NORMAL, state=RecordState.PENDING, has_card=True,
                            inaccessible_since=MARKED, inaccessible_checked_at=MARKED)


@pytest.fixture
def env():
    clock = Clock()
    clock.now = T0 + timedelta(hours=1)
    repo = InMemoryDriveIndexRepository()
    notifier = Notifier()
    reads = []
    real = repo.inaccessible_records

    async def spy(user_id, limit, **kw):
        reads.append(clock.now)
        return await real(user_id, limit, **kw)
    repo.inaccessible_records = spy

    async def connected_users():
        return ["u"]

    svc = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), drive=FakeDrive(),
                                    users=FakeUsers(enabled={"u": True}), connected_users=connected_users,
                                    notifier=notifier, alert_sink=None, clock=clock, localization=Localization())
    return SimpleNamespace(svc=svc, repo=repo, notifier=notifier, clock=clock, reads=reads)


async def seed(env, waiting):
    await env.repo.upsert_records([closed(f"f{n}") for n in range(waiting)])
    env.repo.states["u"] = DriveSyncState(user_id="u", connection_id="u", root_item_id="root", observed_as_of=MARKED + timedelta(minutes=5),
                                          created_at=T0, last_success_at=env.clock.now,
                                          counters={INACCESSIBLE_COUNTER: waiting,
                                                    INACCESSIBLE_UNNOTIFIED_COUNTER: waiting})


async def test_an_undeliverable_notice_is_not_read_again_the_same_day(env):
    await seed(env, 1)
    await env.svc.fan_out()
    env.clock.advance(600)
    await env.svc.fan_out()
    assert len(env.reads) == 1 and env.notifier.files == 1


async def test_it_is_tried_again_the_next_day(env):
    await seed(env, 1)
    await env.svc.fan_out()
    env.clock.advance(24 * 3600)
    env.repo.states["u"] = DriveSyncState(**{**env.repo.states["u"].__dict__, "last_success_at": env.clock.now})
    await env.svc.fan_out()
    assert len(env.reads) == 2


async def test_a_new_marking_the_same_day_tries_again(env):
    await seed(env, 1)
    await env.svc.fan_out()
    env.clock.advance(600)
    await seed(env, 2)   # the tick counted one more file to tell about
    await env.svc.fan_out()
    assert len(env.reads) == 2


async def test_a_delivered_notice_clears_the_hold(env):
    await seed(env, 1)
    await env.svc.fan_out()
    env.notifier.file_sent = True
    env.clock.advance(24 * 3600)
    env.repo.states["u"] = DriveSyncState(**{**env.repo.states["u"].__dict__, "last_success_at": env.clock.now})
    await env.svc.fan_out()
    assert env.notifier.files == 2 and env.repo.records[("u", "f0")].inaccessible_notified_since == MARKED
