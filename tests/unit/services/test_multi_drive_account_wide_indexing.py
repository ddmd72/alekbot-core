"""Account-wide indexing (MULTI_DRIVE_RFC §11.3, E20; T-4 stage 2, indexing half): on an ACCOUNT feed a new connection
indexes nothing, a chosen scope keeps only the subtrees of its folders (ancestors as skeletons), a delta that leaves the
scope removes the record (`unstored`, never `gone`), a folder moved in is listed, narrowing the scope is a diff (DRIVE_SCOPE_WALK_RFC §5.4), the
whole drive admits a parentless item, and the App Folder default makes no extra provider read."""
from dataclasses import replace
from typing import Any, Dict, List, Tuple

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import EpochPhase, Reason
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService
from tests.support.drive_connection_service_fake import FakeDriveConnectionService, connection
from tests.support.fake_drive_adapter import FakeDriveAdapter

from .drive_content_fakes import FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers

USER, CONN = "usr-1", "conn-1"


class World:
    def __init__(self, feed: FeedScope) -> None:
        self.clock = Clock()
        self.repo = InMemoryDriveIndexRepository()
        self.queue = FakeQueue()
        self.connections = FakeDriveConnectionService()
        self.drive = FakeDriveAdapter("fake_b" if feed is FeedScope.ACCOUNT else "fake_a", feed_scope=feed)
        self.svc = DriveIndexingService(self.drive, self.repo, FakeEmbeddings(), self.queue,
                                        FakeUsers(enabled={USER: True}), lambda lat, lon: None, "test-space/8",
                                        extractors={"parser": FakeExtractor("parser"), "reader": FakeExtractor("reader")},
                                        triage=FakeTriage(), connections=self.connections, clock=self.clock,
                                        monotonic=self.clock.monotonic)
        self.notices: List[Tuple[str, str]] = []
        original = self.svc._mirror_notices

        def spy(records, why):
            records = list(records)
            self.notices += [(r.item_id, why) for r in records]
            return original(records, why)
        self.svc._mirror_notices = spy

    async def start(self, **scope: Any) -> None:
        conn = connection(USER, CONN, ref_tag="b2", drive_identity=f"{self.drive.id_space}:drv-cred-1", kind=self.drive.id_space)
        await self.connections.add(replace(conn, **scope))
        self.handle = (await self.connections.get(USER, CONN)).handle
        self.root = self.drive.tree(self.handle).root.item_id

    def plant(self, parent: str, name: str, folder: bool = False) -> str:
        return self.drive.plant(self.handle, parent, name, b"" if folder else b"x", folder=folder)

    async def scope(self, *, folders=(), whole=False) -> None:
        await self.connections.repo.update_settings(USER, CONN, searched_by_default=True, write_folder_id="",
                                                    scope_folder_ids=list(folders), scope_whole_drive=whole)

    async def settle(self) -> None:
        for _ in range(30):
            before = len(self.queue.tasks)
            await self.svc.tick(ConnectionScope(user_id=USER, connection_id=CONN))
            if not [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]:
                return
            self.clock.advance(60)
        raise AssertionError("did not settle")

    def names(self) -> Dict[str, Any]:
        return {r.name: r for (cid, _), r in self.repo.records.items() if cid == CONN}


@pytest.fixture
async def acct():
    w = World(FeedScope.ACCOUNT)
    await w.start()
    w.rd = w.plant(w.root, "R&D", True)
    w.coimbra = w.plant(w.root, "Coimbra 2024", True)
    w.springfield = w.plant(w.root, "Springfield", True)
    w.plan = w.plant(w.rd, "Tom's plan.docx")
    w.lab = w.drive.plant_parentless(w.handle, "lab-report.pdf", b"x")
    return w


async def test_a_new_account_wide_connection_indexes_nothing(acct):
    await acct.settle()
    assert acct.names() == {}
    assert acct.svc._embeddings.calls == []


async def test_only_the_chosen_subtrees_have_records_and_the_ancestors_are_skeletons(acct):
    projects = acct.plant(acct.root, "Projects", True)
    inner = acct.plant(projects, "Acme", True)          # a chosen folder two levels down
    acct.plant(inner, "Acme work.docx")
    acct.plant(projects, "other.docx")
    await acct.scope(folders=[acct.rd, acct.coimbra, inner])
    await acct.settle()
    names = acct.names()
    assert set(names) == {"R&D", "Coimbra 2024", "Tom's plan.docx", "Projects", "Acme", "Acme work.docx"}
    assert names["Projects"].reason is Reason.SCOPE_ANCESTOR
    assert names["R&D"].reason is not Reason.SCOPE_ANCESTOR
    assert "Springfield" not in names and "lab-report.pdf" not in names


async def test_a_delta_that_moves_a_file_out_of_scope_removes_it_as_unstored(acct):
    await acct.scope(folders=[acct.rd])
    await acct.settle()
    assert "Tom's plan.docx" in acct.names()
    acct.notices.clear()
    await acct.drive.move(acct.handle, acct.plan, acct.springfield)
    await acct.settle()
    assert "Tom's plan.docx" not in acct.names()
    assert (acct.plan, svc_mod.UNSTORED) in acct.notices
    assert all(why != svc_mod.GONE for _, why in acct.notices)


async def test_a_folder_moved_into_the_scope_is_listed(acct):
    inside = acct.plant(acct.springfield, "Birth", True)
    acct.plant(inside, "birth-certificate.pdf")
    await acct.scope(folders=[acct.rd])
    await acct.settle()
    assert "birth-certificate.pdf" not in acct.names()
    await acct.drive.move(acct.handle, acct.springfield, acct.rd)
    await acct.settle()
    names = acct.names()
    assert {"Springfield", "Birth", "birth-certificate.pdf"} <= set(names)


async def test_narrowing_the_scope_is_a_diff_that_removes_the_rest(acct):
    """DRIVE_SCOPE_WALK_RFC §5.4, §5.6: narrowing removes the removed root's subtree under the guard, with no
    re-enumeration (the epoch stays) and the kept subtree untouched (same `seen_epoch`)."""
    acct.plant(acct.coimbra, "photo.jpg")
    await acct.scope(folders=[acct.rd, acct.coimbra])
    await acct.settle()
    assert "photo.jpg" in acct.names()
    before = await acct.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN))
    kept = {n: acct.names()[n].seen_epoch for n in ("R&D", "Tom's plan.docx")}
    await acct.scope(folders=[acct.rd])
    await acct.settle()
    state = await acct.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN))
    assert (state.epoch, state.epoch_phase) == (before.epoch, EpochPhase.IDLE)   # no re-enumeration, no sweep
    assert set(acct.names()) == {"R&D", "Tom's plan.docx"}
    assert {n: acct.names()[n].seen_epoch for n in kept} == kept
    assert state.applied_scope == (await acct.connections.get(USER, CONN)).scope_key
    assert state.applied_roots == (acct.rd,)


async def test_the_whole_drive_admits_a_parentless_item(acct):
    await acct.scope(whole=True)
    await acct.settle()
    names = acct.names()
    assert "lab-report.pdf" in names and "Springfield" in names


async def test_the_app_folder_default_indexes_as_before_with_no_extra_change_reads():
    w = World(FeedScope.AREA)
    await w.start()
    folder = w.plant(w.root, "R&D", True)
    w.plant(folder, "Tom's plan.docx")
    await w.settle()
    assert set(w.names()) == {"R&D", "Tom's plan.docx"}
    assert "get_change" not in {m for m, _ in w.drive.calls}
    state = await w.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN))
    assert state.applied_scope == ""


async def test_a_held_record_whose_chain_leaves_the_scope_is_removed_as_out_of_scope(acct):
    """`_resolve_step` on a filtered scope: an unresolved record whose parent chain reaches the root without a chosen
    folder goes as `unstored`, not as an orphan (`gone`)."""
    from src.domain.drive_index import DriveIndexRecord, RecordState
    from src.services.drive_indexing_service import _Budget, _ScopeView, _TICK_SCOPE
    from src.domain.drive_index_scope import IndexScope
    await acct.scope(folders=[acct.rd])
    await acct.settle()
    stray = acct.plant(acct.springfield, "birth-certificate.pdf")
    held = DriveIndexRecord(user_id=USER, connection_id=CONN, item_id=stray, parent_id=acct.springfield,
                            name="birth-certificate.pdf", state=RecordState.UNRESOLVED)
    await acct.repo.upsert_records([held])
    conn = await acct.connections.get(USER, CONN)
    token = _TICK_SCOPE.set(_ScopeView(IndexScope.of(conn, await acct.drive.capabilities(acct.handle))))
    try:
        state = await acct.repo.get_sync_state(ConnectionScope(user_id=USER, connection_id=CONN))
        ctx = acct.svc._ctx(acct.handle, state, acct.clock())
        acct.notices.clear()
        assert await acct.svc._resolve_step(acct.handle, held, state, ctx,
                                            _Budget(started=0.0, monotonic=acct.clock.monotonic))
    finally:
        _TICK_SCOPE.reset(token)
    assert "birth-certificate.pdf" not in acct.names()
    assert acct.notices == [(stray, svc_mod.UNSTORED)]
