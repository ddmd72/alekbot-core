"""`DriveSearchService.indexing_status` fills the Cabinet's unreadable list (USER_DRIVE_INDEXING_RFC §6;
decisions/drive_unreadable_files_cabinet_only.md): total = `unread` minus `not_read_yet`, up to CABINET_UNREADABLE_MAX
files, the kind sets of the indexer's counter (recordings only with the kill switch AND the connection's flag), and a
failed read leaves the list empty while the rest of the status returns."""
from dataclasses import replace

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain import drive_index_policy as policy
from src.domain.drive_index import DriveIndexRecord, DriveSyncState, Reason, RecordState
from src.domain.drive_index_params import CABINET_UNREADABLE_MAX
from src.domain.drive_unreadable_file import UnreadableFile
from src.domain.drive_user_settings import DriveUserSettings
from src.services.drive_connection_service import DriveConnectionService
from src.services.drive_search_service import DriveSearchService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository

from .drive_index_fakes import FakeEmbeddings
from .test_drive_core_reads_2b import T0, connection


class SpyRepo(InMemoryDriveIndexRepository):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.fail = False

    async def unreadable_records(self, scope, kinds, triaged=frozenset(), *, transcribed=frozenset(), limit):
        self.calls.append({"kinds": frozenset(kinds), "triaged": frozenset(triaged),
                           "transcribed": frozenset(transcribed), "limit": limit})
        if self.fail:
            raise RuntimeError("store down")
        return await super().unreadable_records(scope, kinds, triaged, transcribed=transcribed, limit=limit)


def rec(item_id, **kw):
    return DriveIndexRecord(**{"user_id": "usr-1", "connection_id": "conn-1", "item_id": item_id, "kind": "pdf",
                               "state": RecordState.FAILED, **kw})


async def _service(counters, *, records=(), switch=True, transcribe=True):
    conns = FakeDriveConnectionRepository()
    await conns.create(replace(connection("conn-1", "a1"), transcribe_enabled=transcribe))
    await conns.save_settings(DriveUserSettings(user_id="usr-1", primary_connection_id="conn-1"))
    repo = SpyRepo()
    await repo.save_sync_state(DriveSyncState(user_id="usr-1", connection_id="conn-1", epoch=1, root_item_id="root",
                                              created_at=T0, counters=counters))
    await repo.upsert_records(list(records))
    service = DriveSearchService(repo, FakeEmbeddings(), object(), DriveConnectionService(conns, None),
                                 clock=lambda: T0, embedding_space="fake/8", transcribe_switch_on=switch)
    return service, repo


async def test_the_list_and_the_total_are_filled():
    records = [rec("a", name="scan_0012.pdf", web_url="https://example.test/a"),
               rec("b", name="scan_0013.pdf", state=RecordState.METADATA_ONLY, reason=Reason.NO_TEXT)]
    service, repo = await _service({"unread": 5, "not_read_yet": 3}, records=records)
    status = await service.indexing_status("usr-1")
    assert status.unreadable_total == 2
    assert status.unreadable == (UnreadableFile("scan_0012.pdf", "failed", "https://example.test/a"),
                                 UnreadableFile("scan_0013.pdf", "no_text", ""))
    assert repo.calls[0]["limit"] == CABINET_UNREADABLE_MAX
    assert repo.calls[0]["kinds"] == policy.ADMITTED_KINDS and repo.calls[0]["triaged"] == policy.TRIAGED_KINDS


async def test_the_total_can_exceed_the_list():
    records = [rec(f"r{i}", name=f"scan_{i:04d}.pdf") for i in range(CABINET_UNREADABLE_MAX + 2)]
    service, _ = await _service({"unread": len(records)}, records=records)
    status = await service.indexing_status("usr-1")
    assert status.unreadable_total == CABINET_UNREADABLE_MAX + 2 and len(status.unreadable) == CABINET_UNREADABLE_MAX


@pytest.mark.parametrize("counters", [{}, {"unread": 3, "not_read_yet": 3}, {"unread": 1, "not_read_yet": 4}])
async def test_a_zero_total_reads_nothing(counters):
    service, repo = await _service(counters, records=[rec("a")])
    status = await service.indexing_status("usr-1")
    assert status.unreadable == () and status.unreadable_total == 0 and repo.calls == []


async def test_a_failed_read_leaves_the_list_empty_and_the_status_returns(caplog):
    service, repo = await _service({"unread": 2, "carded": 7}, records=[rec("a")])
    repo.fail = True
    status = await service.indexing_status("usr-1")
    assert status.unreadable == () and status.unreadable_total == 2
    assert status.enabled is True and status.counters["carded"] == 7


@pytest.mark.parametrize("switch, flag, passed", [(True, True, True), (True, False, False), (False, True, False),
                                                  (False, False, False)])
async def test_recordings_are_passed_only_with_the_switch_and_the_connection_flag(switch, flag, passed):
    service, repo = await _service({"unread": 1}, records=[rec("a")], switch=switch, transcribe=flag)
    await service.indexing_status("usr-1")
    assert repo.calls[0]["transcribed"] == (policy.TRANSCRIBED_KINDS if passed else frozenset())
