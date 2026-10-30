"""Second review of S1 (USER_DRIVE_INDEXING_RFC §26.2, Δ78, Δ71):
- a sub-folder marked with its folder is due for its own re-read within INACCESSIBLE_RETRY_MIN, not after the 7-day
  timer: one that the app can read shows the access line for at most an hour;
- the fan-out reads the marked records lean, and the lean fields carry everything the notice reads (the path needs the
  record's scope and state to be visible)."""
from datetime import timedelta
from types import SimpleNamespace

import pytest

from src.adapters.firestore_drive_index_repo import ACCESS_MARK_FIELDS
from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.drive_access import notice_batch, notice_paths
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, RecordState, Scope, inaccessible
from src.domain.drive_index_params import INACCESSIBLE_RETRY_MIN
from src.domain.drive_search import INACCESSIBLE_COUNTER, INACCESSIBLE_UNNOTIFIED_COUNTER
from src.domain.ui_messages import UIMessage
from src.services.drive_sync_scheduler_service import DriveSyncSchedulerService

from .drive_content_world import admitted, prose  # noqa: F401  (fixture)
from .drive_index_fakes import T0, Clock, FakeDrive, FakeQueue, FakeUsers
from .drive_inaccessible_world import ClosedWorld

MARKED = T0 + timedelta(minutes=1)


@pytest.mark.usefixtures("admitted")
async def test_a_sub_folder_marked_with_its_folder_is_re_read_within_the_hour():
    w = ClosedWorld()
    outer = w.drive.add("Shared", folder=True)
    inner = w.drive.add("Scans", parent=outer, folder=True)
    doc = w.add_doc("scan.md", prose(40), parent=outer)
    w.drive.closed.update({outer, inner, doc})
    await w.settle()
    rec = w.rec(inner)
    assert inaccessible(rec) and rec.inaccessible_reobserved
    assert rec.due_at <= rec.inaccessible_checked_at + INACCESSIBLE_RETRY_MIN
    w.drive.closed.discard(inner)   # the sub-folder has its own grant for the app
    w.clock.advance(INACCESSIBLE_RETRY_MIN.total_seconds() + 60)
    await w.settle()
    assert not inaccessible(w.rec(inner)) and inaccessible(w.rec(outer))


def _lean(rec: DriveIndexRecord) -> DriveIndexRecord:
    return DriveIndexRecord(**{name: getattr(rec, name) for name in ACCESS_MARK_FIELDS})


def test_the_lean_fields_carry_what_the_notice_reads():
    folder = DriveIndexRecord(user_id="u", connection_id="u", item_id="d1", name="Shared", is_folder=True, kind="folder",
                              scope=Scope.NORMAL, state=RecordState.METADATA_ONLY, parent_id="root")
    doc = DriveIndexRecord(user_id="u", connection_id="u", item_id="f1", name="a.pdf", parent_id="d1", kind="pdf", scope=Scope.NORMAL,
                           state=RecordState.PENDING, inaccessible_since=MARKED)
    lean = _lean(doc)
    after = MARKED + timedelta(minutes=1)
    assert [r.item_id for r in notice_batch([lean], after, 10)] == [r.item_id for r in notice_batch([doc], after, 10)]
    lookup = {"d1": folder}.get
    assert notice_paths([lean], lookup, "root") == notice_paths([doc], lookup, "root") == ["Shared/a.pdf"]


async def test_the_fan_out_reads_the_marked_records_lean():
    clock = Clock()
    clock.now = T0 + timedelta(hours=1)
    repo = InMemoryDriveIndexRepository()
    await repo.upsert_records([DriveIndexRecord(user_id="u", connection_id="u", item_id="f1", name="a.pdf", parent_id="root", kind="pdf",
                                                scope=Scope.NORMAL, state=RecordState.PENDING,
                                                inaccessible_since=MARKED, inaccessible_checked_at=MARKED)])
    repo.states["u"] = DriveSyncState(user_id="u", connection_id="u", root_item_id="root", observed_as_of=MARKED + timedelta(minutes=5),
                                      created_at=T0, last_success_at=clock.now,
                                      counters={INACCESSIBLE_COUNTER: 1, INACCESSIBLE_UNNOTIFIED_COUNTER: 1})
    calls = []
    real = repo.inaccessible_records

    async def spy(user_id, limit, **kw):
        calls.append(kw)
        return await real(user_id, limit, **kw)
    repo.inaccessible_records = spy
    notifier = SimpleNamespace(files=[])

    async def notify_file_bytes(**kw):
        notifier.files.append(kw)
        return True

    async def notify_raw(*a, **kw):
        return True
    notifier.notify_file_bytes, notifier.notify_raw = notify_file_bytes, notify_raw
    localization = SimpleNamespace(get_ui_string=lambda lang, m: "{count} closed"
                                   if m is UIMessage.DRIVE_INACCESSIBLE_NOTICE else "Closed")

    async def connected_users():
        return ["u"]

    svc = DriveSyncSchedulerService(repo=repo, queue=FakeQueue(), drive=FakeDrive(),
                                    users=FakeUsers(enabled={"u": True}), connected_users=connected_users,
                                    notifier=notifier, alert_sink=None, clock=clock, localization=localization)
    await svc.fan_out()
    assert calls == [{"lean": True}]
    assert notifier.files[0]["file_bytes"] == b"a.pdf\n"
