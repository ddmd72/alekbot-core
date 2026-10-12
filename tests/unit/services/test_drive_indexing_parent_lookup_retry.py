"""Parent lookups (USER_DRIVE_INDEXING_RFC §3.4, §8.2). Delta mode has no later full enumeration to settle a record
whose parent lookup keeps failing, so it is retried at RETRY_CAP, never deleted on a transient failure."""
from src.domain import drive_index_planner as planner
from src.domain.drive_index import DriveChange, DriveIndexRecord, Priority, RecordState, Scope
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP
from src.domain.user_drive import DriveAccessDenied, DriveItemNotFoundError

from .drive_index_fakes import ROOT
from .test_drive_indexing_service import World


async def _onboarded():
    w = World()
    w.drive.add("anchor.pdf")
    await w.settle()
    return w


async def _seed_unresolved(w, item_id, name, parent_id, *, folder=False):
    rec = DriveIndexRecord(user_id="u", item_id=item_id, parent_id=parent_id, name=name, is_folder=folder,
                           kind="folder" if folder else "pdf", scope=Scope.UNRESOLVED, state=RecordState.UNRESOLVED,
                           policy_version=planner.POLICY_VERSION, seen_epoch=1, due_at=w.clock.now,
                           priority=int(Priority.CASCADE))
    await w.repo.upsert_records([rec])


class TestTransientParentLookupKeepsRetrying:

    async def test_exhausted_lookups_retry_at_the_cap_and_resolve_once_the_provider_recovers(self):
        w = await _onboarded()
        w.drive.items["far"] = DriveChange(item_id="far", name="Far", parent_id=ROOT, is_folder=True)
        await _seed_unresolved(w, "kid", "plan.pdf", "far")
        lookups = []
        failing = {"on": True}
        real_get = w.drive.get_change

        async def flaky(user_id, item_id):
            lookups.append(item_id)
            if failing["on"]:
                raise ValueError("Graph 503 on the parent lookup")
            return await real_get(user_id, item_id)

        w.drive.get_change = flaky
        for _ in range(MAX_ATTEMPTS + 3):
            await w.settle()
            kid = w.rec("kid")
            assert kid is not None and kid.state is RecordState.UNRESOLVED
            assert kid.due_at is not None and kid.due_at > w.clock.now
            w.clock.advance(RETRY_CAP.total_seconds())
        assert len(lookups) == MAX_ATTEMPTS + 3
        assert w.rec("kid").due_at - w.clock.now <= RETRY_CAP
        failing["on"] = False
        await w.settle()
        kid = w.rec("kid")
        assert kid.state is not RecordState.UNRESOLVED and kid.scope is Scope.NORMAL
        assert w.rec("far") is not None and kid.attempts == 0

    async def test_a_not_found_raised_by_the_lookup_removes_the_orphan_at_once(self):
        w = await _onboarded()
        await _seed_unresolved(w, "orphan", "Projects", "far", folder=True)
        lookups = []

        async def not_found(user_id, item_id):
            lookups.append(item_id)
            raise DriveItemNotFoundError(item_id)

        w.drive.get_change = not_found
        await w.settle()
        assert lookups == ["far"]
        assert w.rec("orphan") is None

    async def test_access_denied_is_transient_and_never_deletes(self):
        w = await _onboarded()
        await _seed_unresolved(w, "orphan", "Projects", "far", folder=True)

        async def denied(user_id, item_id):
            raise DriveAccessDenied("lookup refused")

        w.drive.get_change = denied
        for _ in range(MAX_ATTEMPTS + 2):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())
        assert w.rec("orphan") is not None and w.rec("orphan").state is RecordState.UNRESOLVED


async def _photos_waiting_for_their_probe(w):
    """A carded folder grows past the density threshold and its density probe keeps failing: it waits unresolved
    (probe pending) while its children are still carded."""
    f = w.drive.add("Photos", folder=True)
    for i in range(10):
        w.drive.add(f"p{i}.pdf", parent=f)
    await w.settle()
    for i in range(10, 610):
        w.drive.add(f"p{i}.pdf", parent=f)
    w.drive.reemit(f)

    async def denied(user_id, folder_id, limit, cursor=None):
        raise DriveAccessDenied("probe refused")

    w.drive.list_children_changes = denied
    await w.settle(max_ticks=100)
    assert w.rec(f).probe_pending and w.rec(f).scope is Scope.UNRESOLVED
    return f
