"""A narrow scope on an account-wide feed (MULTI_DRIVE_RFC §11.3, E87; USER_DRIVE_INDEXING_RFC §7.8): class-1 records
that wait only for an item call no longer keep content (class 2) from running in the same tick; a structural stop on the
cascade budget still holds (Δ19 a); a False scope verdict removes, in the same step, the stored records under the links
of its chain that are certainly out of scope (never a scope folder, a scope ancestor or a mirror root); the work of one
step is bounded; resolve work shows in the tick summary and moves `last_success_at`, never `state.counters`."""
from dataclasses import replace
from typing import Any, Dict, List, Optional

import pytest

from src.adapters.in_memory_drive_index_repo import InMemoryDriveIndexRepository
from src.domain.connection_scope import ConnectionScope
from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_index import DriveIndexRecord, Priority, Reason, RecordState, Scope
from src.domain.drive_index_params import TICK_START_BUDGET
from src.domain.drive_index_planner import schedule
from src.domain.drive_index_scope import IndexScope
from src.services import drive_indexing_service as svc_mod
from src.services.drive_indexing_service import DriveIndexingService, _Budget, _ScopeView, _TICK_SCOPE
from tests.support.drive_connection_service_fake import FakeDriveConnectionService, connection
from tests.support.fake_drive_adapter import FakeDriveAdapter

from .drive_content_fakes import FakeExtractor, FakeTriage
from .drive_index_fakes import Clock, FakeEmbeddings, FakeQueue, FakeUsers

USER, CONN = "usr-1", "conn-1"
SCOPE = ConnectionScope(user_id=USER, connection_id=CONN)
PROSE = b"Quarterly plan for the Porto office: hiring, rent and the travel budget for the spring visits."
_RESOLVE_KEYS = ("resolved", "parked", "out_of_scope_removed", "orphans_removed", "probed", "waiting_item_calls")


class World:
    def __init__(self) -> None:
        self.clock = Clock()
        self.repo = InMemoryDriveIndexRepository()
        self.queue = FakeQueue()
        self.connections = FakeDriveConnectionService()
        self.drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
        self.svc = DriveIndexingService(self.drive, self.repo, FakeEmbeddings(), self.queue,
                                        FakeUsers(enabled={USER: True}), lambda lat, lon: None, "test-space/8",
                                        extractors={"parser": FakeExtractor("parser"), "reader": FakeExtractor("reader")},
                                        triage=FakeTriage(), connections=self.connections, clock=self.clock,
                                        monotonic=self.clock.monotonic)
        self.results: List[Any] = []

    async def start(self) -> None:
        conn = connection(USER, CONN, ref_tag="b2", drive_identity=f"{self.drive.id_space}:drv-cred-1",
                          kind=self.drive.id_space)
        await self.connections.add(conn)
        self.handle = (await self.connections.get(USER, CONN)).handle
        self.root = self.drive.tree(self.handle).root.item_id

    def plant(self, parent: str, name: str, folder: bool = False, data: bytes = PROSE) -> str:
        return self.drive.plant(self.handle, parent, name, b"" if folder else data, folder=folder)

    async def scope(self, *folders: str) -> None:
        await self.connections.repo.update_settings(USER, CONN, searched_by_default=True, write_folder_id="",
                                                    scope_folder_ids=list(folders), scope_whole_drive=False)

    async def tick(self) -> Any:
        result = await self.svc.tick(SCOPE)
        self.results.append(result)
        return result

    async def settle(self, max_ticks: int = 30) -> None:
        for _ in range(max_ticks):
            before = len(self.queue.tasks)
            await self.tick()
            if not [t for t in self.queue.tasks[before:] if ":cont:" in (t["key"] or "")]:
                return
            self.clock.advance(60)
        raise AssertionError("did not settle")

    def rec(self, item_id: str) -> Optional[DriveIndexRecord]:
        return self.repo.records.get((CONN, item_id))

    def mine(self) -> List[DriveIndexRecord]:
        return [r for (cid, _), r in self.repo.records.items() if cid == CONN]

    def unresolved(self) -> List[DriveIndexRecord]:
        return [r for r in self.mine() if r.state is RecordState.UNRESOLVED]

    def carded(self, item_id: str) -> bool:
        rec = self.rec(item_id)
        return rec is not None and rec.state is RecordState.INDEXED and rec.has_card

    async def hold(self, item_id: str, parent_id: str, name: str, *, folder: bool = False) -> DriveIndexRecord:
        """A record stored the way an observation stores one whose chain could not be read: UNRESOLVED, due in class 1."""
        rec = schedule(DriveIndexRecord(user_id=USER, connection_id=CONN, item_id=item_id, parent_id=parent_id,
                                        name=name, is_folder=folder, state=RecordState.UNRESOLVED,
                                        scope=Scope.UNRESOLVED), self.clock(), first_seen=True)
        assert rec.priority == int(Priority.CASCADE)
        await self.repo.upsert_records([rec])
        return rec

    def change_reads(self) -> int:
        return sum(1 for m, _ in self.drive.calls if m == "get_change")

    async def state(self):
        return await self.repo.get_sync_state(SCOPE)


@pytest.fixture
async def w():
    world = World()
    await world.start()
    world.rd = world.plant(world.root, "R&D", True)
    world.plan = world.plant(world.rd, "Tom's plan.md")
    return world


def _plant_backlog(w: World, parents: int, files: int) -> Dict[str, List[str]]:
    """Out-of-scope folders under the root, each with files; planted before the round that judges them, so no later
    delta names them again."""
    planted: Dict[str, List[str]] = {}
    for i in range(parents):
        folder = w.plant(w.root, f"Springfield {i}", True)
        planted[folder] = [w.plant(folder, f"photo {i}-{j}.jpg") for j in range(files)]
    return planted


async def _hold_backlog(w: World, planted: Dict[str, List[str]]) -> Dict[str, List[str]]:
    """The planted files stored unresolved under their folder, which the index does not hold (out of scope): the live
    state an enumeration whose item calls ran out leaves behind."""
    for folder, items in planted.items():
        assert w.rec(folder) is None
        for item in items:
            await w.hold(item, folder, f"held {item}")
    return planted


# -- 1. no starvation ---------------------------------------------------------------------------------------------

async def test_class_one_waiting_only_for_item_calls_does_not_starve_content(w, monkeypatch):
    """Every class-1 record waits for an item call (none left): the same tick goes on to class 2 and cards the new
    in-scope file; the held records are left as they were."""
    planted = _plant_backlog(w, parents=3, files=2)
    await w.scope(w.rd)
    await w.settle()
    assert w.carded(w.plan)
    held = await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    fresh = w.plant(w.rd, "Coimbra 2024 budget.md")
    w.clock.advance(60)
    result = await w.tick()
    assert w.carded(fresh), "class 2 content waited behind class-1 records that only wanted item calls"
    assert {r.item_id for r in w.unresolved()} == {i for items in held.values() for i in items}
    assert result.summary.get("waiting_item_calls", 0) > 0
    assert w.queue.tasks and ":cont:" in (w.queue.tasks[-1]["key"] or "")   # the held records still want a tick


async def test_more_than_a_page_of_waiting_records_still_lets_content_run(w, monkeypatch):
    """The waiting head is longer than one due page: content still runs in the same tick."""
    planted = _plant_backlog(w, parents=1, files=svc_mod._DUE_PAGE + 5)
    await w.scope(w.rd)
    await w.settle()
    await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    fresh = w.plant(w.rd, "Coimbra 2024 budget.md")
    w.clock.advance(60)
    await w.tick()
    assert w.carded(fresh)


@pytest.mark.parametrize("structural", ["deleting", "rescope"])
async def test_a_structural_class_one_stop_still_holds_content(w, monkeypatch, structural):
    """Regression guard (Δ19 a): a class-1 record stopped on the cascade budget (a folder delete or a rescope) keeps
    class 2 waiting, also when other class-1 records of the page only wait for item calls."""
    planted = _plant_backlog(w, parents=1, files=2)
    await w.scope(w.rd)
    await w.settle()
    old = w.plant(w.rd, "Old", True)
    w.clock.advance(60)
    await w.settle()
    folder = w.rec(old)
    assert folder is not None and folder.state is RecordState.METADATA_ONLY
    if structural == "deleting":
        stuck = replace(folder, state=RecordState.DELETING)
    else:
        stuck = replace(folder, scope=Scope.OPAQUE_ROOT, reason=Reason.OPAQUE_ROOT, rescope_pending=True)
    await w.repo.upsert_records([schedule(stuck, w.clock(), first_seen=False)])
    await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 0)
    fresh = w.plant(w.rd, "Coimbra 2024 budget.md")
    w.clock.advance(60)
    await w.tick()
    rec = w.rec(fresh)
    assert rec is not None and rec.state is RecordState.PENDING and not rec.has_card, \
        "content ran while a class-1 structural step was stopped on its budget"


async def test_a_waiting_probe_with_a_rescope_pending_still_holds_its_childrens_content(w, monkeypatch):
    """A folder whose density decision is re-opened (probe and rescope pending) waits for an item call: its stored
    children are still to be re-classified, so their content waits (Δ19 a); the item-call skip does not apply to it."""
    big = w.plant(w.rd, "Big", True)
    await w.scope(w.rd)
    await w.settle()
    child = w.plant(big, "draft.md")
    monkeypatch.setattr(svc_mod, "TICK_FILE_BUDGET", 0)    # observed, not read yet
    w.clock.advance(60)
    await w.tick()
    assert w.rec(child).state is RecordState.PENDING
    monkeypatch.undo()
    folder = w.rec(big)
    reopened = replace(folder, scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED, reason=None, probe_pending=True,
                       rescope_pending=True)
    first_seen = schedule(w.rec(child), w.clock(), first_seen=True)   # its content due in class 2
    assert first_seen.priority == int(Priority.FIRST_SEEN)
    await w.repo.upsert_records([schedule(reopened, w.clock(), first_seen=False), first_seen])
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    w.clock.advance(60)
    await w.tick()
    rec = w.rec(child)
    assert rec.state is RecordState.PENDING and rec.indexed_generation is None, \
        "a child of a folder whose rescope is pending was read"


async def test_waiting_records_keep_the_upgrade_pass_from_running(w, monkeypatch):
    """A class skipped for want of item calls counts as a class stopped on its budget: the upgrade step and the cap
    releases wait (Δ30)."""
    planted = _plant_backlog(w, parents=1, files=1)
    await w.scope(w.rd)
    await w.settle()
    await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    ran: List[str] = []

    async def upgrade(*args: Any, **kwargs: Any) -> Any:
        ran.append("upgrade")
        return args[1]
    monkeypatch.setattr(w.svc, "_upgrade_step", upgrade)
    w.clock.advance(60)
    await w.tick()
    assert ran == []


# -- 2. the out-of-scope drain ------------------------------------------------------------------------------------

async def _step_world(w: World, *scope_folders: str):
    """The tick's scope view and plan context, as a tick sets them, for a direct `_resolve_step`."""
    conn = await w.connections.get(USER, CONN)
    view = _ScopeView(IndexScope.of(conn, await w.drive.capabilities(w.handle)))
    token = _TICK_SCOPE.set(view)
    state = await w.state()
    ctx = w.svc._ctx(w.handle, state, w.clock())
    budget = _Budget(started=0.0, monotonic=w.clock.monotonic)
    await w.svc._load_scope_ancestors(w.handle, view, ctx, budget)
    ctx = replace(ctx, scope_ancestor_ids=frozenset(view.ancestors))
    return view, token, state, ctx, budget


async def test_a_false_verdict_removes_its_siblings_under_the_absent_parent_in_one_step(w):
    planted = _plant_backlog(w, parents=1, files=4)
    await w.scope(w.rd)
    await w.settle()
    held = await _hold_backlog(w, planted)
    (parent, items), = held.items()
    view, token, state, ctx, budget = await _step_world(w)
    summary: Dict[str, int] = {}
    reads = w.change_reads()
    try:
        moved = await w.svc._resolve_step(w.handle, w.rec(items[0]), state, ctx, budget, summary)
    finally:
        _TICK_SCOPE.reset(token)
    assert moved is True
    assert all(w.rec(i) is None for i in items)
    assert w.change_reads() - reads == 1   # the absent parent, read once
    assert summary["out_of_scope_removed"] == 4
    assert w.carded(w.plan)


async def test_the_drain_never_removes_scope_folders_their_ancestors_mirror_roots_or_in_scope_records(w):
    """The safety rule: a link of a False chain may be an ancestor of a scope folder. Under it, a scope folder, an
    ancestor of one and a mirror root stay, and so does everything under the scope folder; the rest goes."""
    projects = w.plant(w.root, "Projects", True)
    acme = w.plant(projects, "Acme", True)              # an ancestor of a scope folder, a child of the link
    team = w.plant(acme, "Team", True)                  # a scope folder below it
    notes = w.plant(team, "Acme work.md")               # in scope, under the scope folder
    plans = w.plant(projects, "Plans", True)            # a scope folder, a direct child of the link
    plan_doc = w.plant(plans, "Plans 2024.md")
    await w.scope(w.rd, team, plans)
    await w.settle()
    assert w.rec(projects).reason is Reason.SCOPE_ANCESTOR and w.rec(acme).reason is Reason.SCOPE_ANCESTOR
    transcripts = w.plant(projects, "_Transcripts", True)   # a mirror root under the link
    await w.repo.upsert_records([DriveIndexRecord(user_id=USER, connection_id=CONN, item_id=transcripts,
                                                  parent_id=projects, name="_Transcripts", is_folder=True,
                                                  state=RecordState.METADATA_ONLY, scope=Scope.OPAQUE_ROOT,
                                                  reason=Reason.OPAQUE_ROOT)])
    stray = w.plant(projects, "lab-report.pdf")             # out of scope, directly under the link
    await w.hold(stray, projects, "lab-report.pdf")
    beta = w.plant(projects, "Beta", True)                  # out of scope, never stored
    b1, b2 = w.plant(beta, "b1.md"), w.plant(beta, "b2.md")
    await w.hold(b1, beta, "b1.md")
    await w.hold(b2, beta, "b2.md")
    keep = {projects, acme, team, notes, plans, plan_doc, transcripts, w.rd, w.plan}
    assert all(w.rec(i) is not None for i in keep)
    view, token, state, ctx, budget = await _step_world(w)
    ctx = replace(ctx, mirror_root_ids=frozenset({transcripts}))
    try:
        assert view.loaded and {projects, acme} <= view.ancestors
        assert await w.svc._resolve_step(w.handle, w.rec(b1), state, ctx, budget, {}) is True
    finally:
        _TICK_SCOPE.reset(token)
    assert w.rec(b1) is None and w.rec(b2) is None and w.rec(stray) is None
    assert all(w.rec(i) is not None and w.rec(i).state is not RecordState.DELETING for i in keep)


async def test_no_drain_while_the_scope_ancestry_is_incomplete(w):
    """Without the full ancestry the rule cannot tell an ancestor from an out-of-scope folder: only the record goes."""
    planted = _plant_backlog(w, parents=1, files=3)
    await w.scope(w.rd)
    await w.settle()
    held = await _hold_backlog(w, planted)
    (_, items), = held.items()
    view, token, state, ctx, budget = await _step_world(w)
    view.loaded = False
    try:
        assert await w.svc._resolve_step(w.handle, w.rec(items[0]), state, ctx, budget, {}) is True
    finally:
        _TICK_SCOPE.reset(token)
    assert w.rec(items[0]) is None and all(w.rec(i) is not None for i in items[1:])


def _record(w: World, item_id: str, parent_id: str, name: str, state: RecordState, *, folder: bool = False,
            **fields: Any) -> DriveIndexRecord:
    scope = Scope.UNRESOLVED if state is RecordState.UNRESOLVED else Scope.NORMAL
    return DriveIndexRecord(user_id=USER, connection_id=CONN, item_id=item_id, parent_id=parent_id, name=name,
                            is_folder=folder, state=state, scope=scope, **fields)


@pytest.mark.parametrize("feed", ["scope_folder_moved", "file_only"])
async def test_no_drain_in_a_tick_whose_feed_moved_a_folder(w, feed):
    """The review's U1 order: the same tick's feed moves a scope folder under a stored, parked out-of-scope folder W
    (root/Z/W/S), then a False verdict under Z runs. Nothing under the scope folder goes, W is not touched, and nothing is
    drained that tick: the parked sibling stays (only its own step, not due, could remove it). With a feed that changed
    only a file the same sibling is drained."""
    projects = w.plant(w.root, "Projects", True)
    acme = w.plant(projects, "Acme", True)
    work = w.plant(acme, "Acme work.md")
    z = w.plant(w.root, "Springfield 0", True)
    year = w.plant(z, "Year 1", True)
    r, r2 = w.plant(z, "photo 0-0.jpg"), w.plant(z, "photo 0-1.jpg")
    await w.scope(w.rd, acme)
    await w.settle()
    assert w.carded(work) and w.rec(z) is None and w.rec(year) is None
    parked = [_record(w, year, z, "Year 1", RecordState.UNRESOLVED, folder=True),
              _record(w, r2, z, "photo 0-1.jpg", RecordState.UNRESOLVED)]   # not due: no step of its own this tick
    await w.repo.upsert_records(parked)
    await w.hold(r, z, "photo 0-0.jpg")
    if feed == "scope_folder_moved":
        await w.drive.move(w.handle, acme, year)
    else:
        w.plant(w.rd, "Coimbra 2024 budget.md")
    w.clock.advance(60)
    result = await w.tick()
    assert w.rec(r) is None   # its own step: removed as out of scope, as before E87
    assert w.rec(acme) is not None and w.rec(acme).state is not RecordState.DELETING
    assert w.rec(work) is not None and w.rec(work).state is RecordState.INDEXED
    assert w.rec(year) is not None and w.rec(year).state is RecordState.UNRESOLVED
    if feed == "scope_folder_moved":
        assert w.rec(r2) is not None, "drained in a tick whose feed moved a folder"
        assert result.summary.get("out_of_scope_removed") == 1
    else:
        assert w.rec(r2) is None
        assert result.summary.get("out_of_scope_removed") == 2


@pytest.mark.parametrize("feed", ["scope_folder_moved", "file_only"])
async def test_a_folder_judged_false_waits_for_a_fresh_ancestry_when_the_feed_moved_a_folder(w, feed):
    """The per-record U1 (on `main` before E87): the same tick's feed moves a scope folder S under a stored, due,
    unresolved folder W (root/Z/W/S). W's own verdict, taken on the tick's stale ancestry, is False; W stays as it was
    (due, not removed, not progress) and S's subtree is untouched. The next tick reads the ancestry again, finds W above
    S, and keeps W. Control: with a feed that changed only a file, an out-of-scope unresolved folder leaves as before."""
    projects = w.plant(w.root, "Projects", True)
    acme = w.plant(projects, "Acme", True)
    work = w.plant(acme, "Acme work.md")
    z = w.plant(w.root, "Springfield 0", True)
    year = w.plant(z, "Year 1", True)
    await w.scope(w.rd, acme)
    await w.settle()
    assert w.carded(work) and w.rec(year) is None
    held = await w.hold(year, z, "Year 1", folder=True)
    if feed == "scope_folder_moved":
        await w.drive.move(w.handle, acme, year)
    else:
        w.plant(w.rd, "Coimbra 2024 budget.md")
    w.clock.advance(60)
    result = await w.tick()
    if feed == "file_only":
        assert w.rec(year) is None or w.rec(year).state is RecordState.DELETING
        assert result.summary.get("out_of_scope_removed") == 1
        return
    after = w.rec(year)
    assert after is not None and after.state is RecordState.UNRESOLVED and after.due_at == held.due_at
    assert "out_of_scope_removed" not in result.summary
    assert w.rec(acme).state is not RecordState.DELETING and w.rec(acme).parent_id == year
    assert w.carded(work)
    assert w.queue.tasks and ":cont:" in (w.queue.tasks[-1]["key"] or "")   # W is still due: the next tick comes
    w.clock.advance(60)
    await w.settle()
    kept = w.rec(year)
    assert kept is not None and kept.state is not RecordState.DELETING
    assert w.rec(acme) is not None and w.rec(acme).state is not RecordState.DELETING and w.carded(work)


async def test_the_drain_takes_only_unresolved_files_also_on_a_stale_chain(w):
    """The review's U2 shape, and the drain's rule: link L was moved into the scope on the drive after the tick read
    its chain (its stored record still names the out-of-scope parent). A False verdict on a file under L drains only the
    unresolved files under L: no folder (unresolved or deleting), and no pending, indexed (carded) or metadata-only
    record is removed."""
    projects = w.plant(w.root, "Projects", True)
    acme = w.plant(projects, "Acme", True)
    z = w.plant(w.root, "Springfield 0", True)
    link = w.plant(z, "Year 1", True)
    names = ["held.jpg", "other.jpg", "pending.md", "carded.md", "metadata.bin", "Sub", "Gone"]
    ids = {n: w.plant(link, n, n in ("Sub", "Gone")) for n in names}
    await w.scope(w.rd, acme)
    await w.settle()
    assert w.rec(link) is None and all(w.rec(i) is None for i in ids.values())
    await w.repo.upsert_records([
        _record(w, link, z, "Year 1", RecordState.UNRESOLVED, folder=True),
        _record(w, ids["other.jpg"], link, "other.jpg", RecordState.UNRESOLVED),
        _record(w, ids["pending.md"], link, "pending.md", RecordState.PENDING),
        _record(w, ids["carded.md"], link, "carded.md", RecordState.INDEXED, has_card=True, indexed_generation=1),
        _record(w, ids["metadata.bin"], link, "metadata.bin", RecordState.METADATA_ONLY),
        _record(w, ids["Sub"], link, "Sub", RecordState.UNRESOLVED, folder=True),
        _record(w, ids["Gone"], link, "Gone", RecordState.DELETING, folder=True),
    ])
    held = await w.hold(ids["held.jpg"], link, "held.jpg")
    view, token, state, ctx, budget = await _step_world(w)
    try:
        view.parents[link] = z   # the tick read L's chain before the drive moved it
        await w.drive.move(w.handle, link, acme)   # out of band: no feed of this tick names it
        summary: Dict[str, int] = {}
        assert await w.svc._resolve_step(w.handle, held, state, ctx, budget, summary) is True
    finally:
        _TICK_SCOPE.reset(token)
    assert w.rec(ids["held.jpg"]) is None and w.rec(ids["other.jpg"]) is None
    assert summary["out_of_scope_removed"] == 2
    kept = {"pending.md": RecordState.PENDING, "carded.md": RecordState.INDEXED,
            "metadata.bin": RecordState.METADATA_ONLY, "Sub": RecordState.UNRESOLVED, "Gone": RecordState.DELETING}
    for name, kept_state in kept.items():
        rec = w.rec(ids[name])
        assert rec is not None and rec.state is kept_state, name
    assert w.rec(ids["carded.md"]).has_card
    assert w.rec(link) is not None and w.rec(link).state is RecordState.UNRESOLVED


@pytest.mark.parametrize("shape", ["absent_parent", "deleting_parent", "unresolved_parent"])
async def test_each_backlog_shape_drains_in_bounded_ticks(w, monkeypatch, shape):
    """The live backlog's three shapes, with one item call per tick: unresolved files under an absent out-of-scope
    folder, under a stored deleting folder, and under a stored unresolved folder whose own parent is absent."""
    z = w.plant(w.root, "Springfield 0", True)
    parent = z if shape == "absent_parent" else w.plant(z, "Year 1", True)
    files = [w.plant(parent, f"photo 0-{j}.jpg") for j in range(12)]
    await w.scope(w.rd)
    await w.settle()
    if shape == "deleting_parent":
        await w.repo.upsert_records([schedule(_record(w, parent, z, "Year 1", RecordState.DELETING, folder=True),
                                              w.clock(), first_seen=False)])
    elif shape == "unresolved_parent":
        await w.hold(parent, z, "Year 1", folder=True)
    for j, item in enumerate(files):
        await w.hold(item, parent, f"photo 0-{j}.jpg")
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 1)
    w.clock.advance(60)
    await w.settle(max_ticks=6)
    assert w.unresolved() == []
    assert {r.name for r in w.mine()} == {"R&D", "Tom's plan.md"}


async def test_the_drain_of_one_step_is_bounded_and_the_next_step_goes_on(w, monkeypatch):
    monkeypatch.setattr(svc_mod, "_DRAIN_PER_STEP", 2)
    planted = _plant_backlog(w, parents=1, files=7)
    await w.scope(w.rd)
    await w.settle()
    held = await _hold_backlog(w, planted)
    (_, items), = held.items()
    view, token, state, ctx, budget = await _step_world(w)
    try:
        await w.svc._resolve_step(w.handle, w.rec(items[0]), state, ctx, budget, {})
        assert len([i for i in items if w.rec(i) is not None]) == 7 - 1 - 2
        reads = w.change_reads()
        left = next(i for i in items if w.rec(i) is not None)
        await w.svc._resolve_step(w.handle, w.rec(left), state, ctx, budget, {})
        assert w.change_reads() == reads   # the chain is this tick's already: no item call
        assert len([i for i in items if w.rec(i) is not None]) == 7 - 1 - 2 - 1 - 2
    finally:
        _TICK_SCOPE.reset(token)


async def test_the_drain_spends_the_cascade_budget(w):
    planted = _plant_backlog(w, parents=1, files=5)
    await w.scope(w.rd)
    await w.settle()
    held = await _hold_backlog(w, planted)
    (_, items), = held.items()
    view, token, state, ctx, budget = await _step_world(w)
    budget.cascade_ops = 2
    try:
        await w.svc._resolve_step(w.handle, w.rec(items[0]), state, ctx, budget, {})
    finally:
        _TICK_SCOPE.reset(token)
    assert len([i for i in items if w.rec(i) is not None]) == 5 - 1 - 2
    assert budget.cascade_ops == 0


# -- 3. observability ---------------------------------------------------------------------------------------------

async def test_resolve_work_moves_last_success_and_stays_out_of_the_counters(w, monkeypatch):
    """A tick that only resolves records ends `continued` (one parent still waits for an item call) and still counts as
    progress; the resolve keys are in the tick summary, never in the stored counters."""
    planted = _plant_backlog(w, parents=2, files=2)
    await w.scope(w.rd)
    await w.settle()
    await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 1)
    before = (await w.state()).last_success_at
    w.clock.advance(60)
    result = await w.tick()
    state = await w.state()
    assert result.summary.get("out_of_scope_removed") == 2 and result.summary.get("waiting_item_calls")
    assert w.unresolved(), "the second parent should still wait"
    assert state.last_success_at == w.clock() != before
    assert not set(_RESOLVE_KEYS) & set(state.counters)


async def test_a_tick_that_only_waits_is_not_progress(w, monkeypatch):
    planted = _plant_backlog(w, parents=1, files=2)
    await w.scope(w.rd)
    await w.settle()
    await _hold_backlog(w, planted)
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 0)
    before = (await w.state()).last_success_at
    w.clock.advance(60)
    result = await w.tick()
    assert result.summary.get("waiting_item_calls")
    assert (await w.state()).last_success_at == before
    assert not set(_RESOLVE_KEYS) & set((await w.state()).counters)


# -- 4. end to end on the account-wide fake (T-4 `fake_b`) -------------------------------------------------------

def _paged_enumeration(w: World, size: int) -> None:
    """The enumeration in pages of `size`, one page per tick (each page spends the start budget, as a slow provider's
    does), so out-of-scope folders decided on one tick are not known on the next: their children are stored
    unresolved under a parent that is absent, as on the live account-wide feed."""
    real = w.drive.get_changes
    full: Dict[str, Any] = {}

    async def paged(handle, cursor):
        if cursor is not None and not cursor.startswith("p:"):
            return await real(handle, cursor)
        if cursor is None:
            full["page"] = await real(handle, None)
        page = full["page"]
        k = int(cursor[2:]) if cursor else 0
        rows = page.changes[k * size:(k + 1) * size]
        last = (k + 1) * size >= len(page.changes)
        w.clock.mono += TICK_START_BUDGET.total_seconds()   # the page used the start budget
        return replace(page, changes=rows, cursor=page.cursor if last else f"p:{k + 1}", complete=last)
    w.drive.get_changes = paged


@pytest.mark.slow
async def test_a_narrow_scope_over_a_large_out_of_scope_tree_drains_while_content_is_carded(monkeypatch):
    monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 3)
    w = World()
    await w.start()
    rd = w.plant(w.root, "R&D", True)
    for i in range(8):
        archive = w.plant(w.root, f"Archive {i}", True)
        for j in range(3):
            year = w.plant(archive, f"Year {j}", True)
            for k in range(6):
                w.plant(year, f"scan {i}-{j}-{k}.pdf")
    inside = [w.plant(rd, f"Tom's plan {n}.md") for n in range(4)]
    await w.scope(rd)
    _paged_enumeration(w, size=7)
    backlog_seen = 0
    carded_with_backlog = False
    for _ in range(80):
        before = len(w.queue.tasks)
        await w.tick()
        backlog = len(w.unresolved())
        backlog_seen = max(backlog_seen, backlog)
        carded_with_backlog |= backlog > 0 and any(w.carded(i) for i in inside)
        if not [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]:
            break
        w.clock.advance(60)
    else:
        raise AssertionError("did not settle")
    assert backlog_seen > 50, "the scenario did not build an unresolved backlog"
    assert carded_with_backlog, "in-scope files waited for the whole backlog"
    assert w.unresolved() == []
    assert all(w.carded(i) for i in inside)
    assert {r.name for r in w.mine()} == {"R&D", *(f"Tom's plan {n}.md" for n in range(4))}
