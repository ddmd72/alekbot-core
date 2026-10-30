"""Regression tests for the planner and the tick (§3.4, §5.2, §7, §12.3) and the scenarios the property
oracle does not generate (§16, §17.1, §17.2): versions, a failing re-listing, a re-enable during a purge,
crash windows, card release, systemic backoff, orphans, a 410 mid-enumeration, density through delta."""
from dataclasses import replace
from datetime import timedelta

import pytest

from src.domain import drive_card, drive_index_planner as planner
from src.domain.drive_index import CARD_VERSION, CURRENT_VERSIONS, EpochPhase, Reason, RecordState, Scope
from src.domain.drive_index_params import TICK_START_BUDGET
from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.exceptions import LLMRateLimitError
from src.domain.user_drive import DriveAccessDenied, DriveResyncRequired
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import U_SCOPE


async def test_card_version_bump_reembeds_and_completes(monkeypatch):
    w = World()
    for i in range(3):
        w.drive.add(f"f{i}.pdf")
    await w.settle()
    monkeypatch.setattr(drive_card, "CARD_VERSION", CARD_VERSION + 1)
    monkeypatch.setattr(planner, "CARD_VERSION", CARD_VERSION + 1)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**CURRENT_VERSIONS, "card": CARD_VERSION + 1})
    w.emb.calls.clear()
    await w.settle(max_ticks=60)          # raises "did not settle" if the upgrade never completes
    assert len(w.emb.calls) == 3
    assert dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions)["card"] == CARD_VERSION + 1


# 2. §8.2: a re-listing that keeps failing is dropped after MAX_ATTEMPTS; the chain must stop.
async def test_persistently_failing_relisting_does_not_loop_forever():
    w = World()
    hidden = w.drive.add(".hidden", folder=True)
    w.drive.add("a.pdf", parent=hidden)
    await w.settle()
    w.drive.rename(hidden, "visible")
    orig = w.drive.list_children_changes

    async def denied(user_id, folder_id, limit, cursor=None):
        if folder_id == hidden:
            raise DriveAccessDenied("refused")
        return await orig(user_id, folder_id, limit, cursor)

    w.drive.list_children_changes = denied
    await w.settle(max_ticks=50)


# 3. Opt-out purge interrupted by a re-enable must not leave a half index with a live cursor.
async def test_reenable_during_a_multi_tick_purge_rebuilds_the_index():
    w = World()
    ids = [w.drive.add(f"f{i}.pdf") for i in range(30)]
    await w.settle()
    w.users.enabled["u"] = False
    orig = w.repo.delete_user_records

    async def slow_delete(user_id, limit):
        w.clock.advance(TICK_START_BUDGET.total_seconds())   # one batch per tick
        return await orig(user_id, 10)

    w.repo.delete_user_records = slow_delete
    assert (await w.tick()).status == "purging"
    w.repo.delete_user_records = orig
    w.users.enabled["u"] = True
    await w.settle()
    assert {i for (_, i) in w.repo.records} == set(ids)


# 4. §5.4: a document never stays capped while a non-document holds a card (release order).
async def test_release_prefers_documents_over_newer_non_documents(monkeypatch):
    monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 3)
    w = World()
    d = [w.drive.add(f"d{i}.pdf") for i in range(3)]
    await w.settle()
    newer = w.clock.now + timedelta(days=1)
    old = w.drive.add("old.pdf")                     # document, capped: every carded record is a document
    w.drive.items[old] = replace(w.drive.items[old], modified_at=w.clock.now - timedelta(days=9))
    w.drive.log[-1] = w.drive.items[old]
    await w.settle()
    new = w.drive.add("new.py")                      # other, capped
    w.drive.items[new] = replace(w.drive.items[new], modified_at=newer)
    w.drive.log[-1] = w.drive.items[new]
    await w.settle()
    w.drive.delete(d[0])
    await w.settle()
    doc = next(r for r in w.repo.records.values() if r.name == "old.pdf")
    code = next(r for r in w.repo.records.values() if r.name == "new.py")
    assert not (code.has_card and not doc.has_card), "a .py holds a card while a PDF is capped"


# 5. Crash window: rescope writes the folder cursor before its deletes land.
async def test_rescope_crash_between_writes_and_deletes_converges():
    w = World()
    site = w.drive.add("site", folder=True)
    files = [w.drive.add(f"m{i}.py", parent=site) for i in range(5)]
    await w.settle()
    w.drive.add(".git", parent=site, folder=True)
    orig = w.repo.delete_records
    crashed = {"done": False}

    async def crash_once(user_id, item_ids):
        if not crashed["done"] and set(item_ids) & set(files):
            crashed["done"] = True
            raise RuntimeError("instance died")
        return await orig(user_id, item_ids)

    w.repo.delete_records = crash_once
    assert (await w.tick()).status == "error"        # the crash ends the tick (error path)
    await w.settle()
    assert all(w.rec(f) is None for f in files), "files under an opaque root kept their records/cards"


# 6. Systemic error after a sweep re-opened a repository: the never-stored subtree must still be listed.
async def test_reopen_survives_a_throttled_embedding():
    w = World()
    site = w.drive.add("site", folder=True)
    git = w.drive.add(".git", parent=site, folder=True)
    a = w.drive.add("a.py", parent=site)
    await w.settle()
    assert w.rec(a) is None
    w.drive.items.pop(git)            # .git gone while the cursor expires: no tombstone reaches us
    w.drive.resync_next = True
    orig = w.emb.get_embedding
    hit = {"n": 0}

    async def throttle_once(text, task_type="RETRIEVAL_DOCUMENT"):
        if hit["n"] == 0:
            hit["n"] = 1
            raise LLMRateLimitError("429")
        return await orig(text, task_type)

    w.emb.get_embedding = throttle_once
    await w.settle()
    assert w.rec(site).scope is Scope.NORMAL
    assert w.rec(a) is not None, "the re-opened repository's files were never listed"


# 7. §3.4: an unresolved folder deleted as an orphan must not leave its parked children behind.
async def test_orphan_folder_takes_parked_children_with_it():
    w = World()
    await w.settle()
    ghost = "ghost-parent"
    orphan = w.drive.add("orphan", parent=ghost, folder=True, item_id="z-orphan")   # parent never observed
    w.drive.add("kid.pdf", parent=orphan, item_id="a-kid")     # sorts first: parked behind its parent
    w.drive.items.pop(orphan)                                      # gone before it resolves
    await w.settle(max_ticks=100)
    w.clock.advance(timedelta(days=1).total_seconds())
    await w.settle(max_ticks=100)
    leftovers = [r for r in w.repo.records.values() if r.state is RecordState.UNRESOLVED]
    assert leftovers == [], [(r.name, r.due_at) for r in leftovers]


# 8. §8.2: a persistent drive-wide error continues "with backoff", not at a fixed 60 s forever.
async def test_systemic_error_backs_off():
    w = World()
    await w.settle()
    delays = []
    for _ in range(4):
        w.drive.raise_next = DriveAccessDenied("feed refused")
        await w.tick()
        delays.append(w.queue.tasks[-1]["delay"])
        w.clock.advance(61)
    assert delays[-1] > delays[0], delays


async def test_resync_in_the_middle_of_the_onboarding_enumeration():
    w = World(page_size=5)
    ids = [w.drive.add(f"f{i}.pdf") for i in range(20)]
    orig = w.drive.get_changes
    fired = {"n": 0}

    async def gone_mid_enumeration(user_id, cursor):
        if cursor == "full:10" and not fired["n"]:
            fired["n"] = 1
            w.drive.items.pop(ids[0])                  # vanished during the gap, no tombstone
            raise DriveResyncRequired("full:0")
        return await orig(user_id, cursor)

    w.drive.get_changes = gone_mid_enumeration
    await w.settle()
    st = await w.repo.get_sync_state(U_SCOPE)
    assert st.epoch_phase is EpochPhase.IDLE and st.epoch == 2
    assert {i for (_, i) in w.repo.records} == set(ids[1:])


@pytest.mark.slow
async def test_folder_growing_past_the_threshold_through_delta_becomes_dense():
    w = World(page_size=200)
    dump = w.drive.add("mirror", folder=True)
    for i in range(10):
        w.drive.add(f"p{i}.bin", parent=dump)
    await w.settle()
    for i in range(10, 610):
        w.drive.add(f"p{i}.bin", parent=dump)
    w.drive.reemit(dump)                               # Graph re-emits the parent with its new childCount
    await w.settle(max_ticks=100)
    assert w.rec(dump).reason is Reason.DENSE_TREE
    assert sum(1 for r in w.repo.records.values() if r.parent_id == dump) == 0
    assert w.carded() == {dump}


async def test_dense_folder_halving_reopens_and_lists_its_files():
    w = World(page_size=200)
    dump = w.drive.add("mirror", folder=True)
    kids = [w.drive.add(f"p{i}.bin", parent=dump) for i in range(610)]
    await w.settle(max_ticks=100)
    assert w.rec(dump).reason is Reason.DENSE_TREE
    for k in kids[:450]:
        w.drive.delete(k)
    w.drive._touch_parent(dump)
    w.drive.reemit(dump)
    await w.settle(max_ticks=100)
    assert w.rec(dump).scope is Scope.NORMAL
    assert sum(1 for r in w.repo.records.values() if r.parent_id == dump) == 160


async def test_nudge_during_sweeping_survives_the_sweep():
    w = World()
    a = w.drive.add("a.pdf")
    await w.settle()
    w.drive.resync_next = True
    b = w.drive.add("b.pdf")
    w.drive.log.pop()
    st = await w.repo.get_sync_state(U_SCOPE)
    await w.repo.save_sync_state(replace(st, epoch_phase=EpochPhase.SWEEPING, epoch=st.epoch + 1))
    from src.domain.drive_index import Nudge
    await w.tick(Nudge("m", (b,)))
    await w.settle()
    assert w.rec(b) is not None and w.rec(a) is not None


@pytest.mark.parametrize("constant,module_attrs", [
    ("policy", [(planner, "POLICY_VERSION")]),
    ("card", [(drive_card, "CARD_VERSION"), (planner, "CARD_VERSION")]),
    ("geo_table", [(planner, "GEO_TABLE_VERSION")]),
])
async def test_every_version_bump_completes_and_applies(monkeypatch, constant, module_attrs):
    """§17.1: after a bump every stale record becomes due, and applied_versions moves only when none is left."""
    w = World()
    for i in range(4):
        w.drive.add(f"IMG_{i}.jpg", media=DriveMediaFacts(latitude=41.9, longitude=12.5))
    await w.settle_due()   # images without a rendition reach their verdict only after retries (review B minor)
    assert all(r.due_at is None for r in w.repo.records.values())
    bumped = CURRENT_VERSIONS[constant] + 1
    for module, attr in module_attrs:
        monkeypatch.setattr(module, attr, bumped)
    target = {**CURRENT_VERSIONS, constant: bumped}
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", target)
    await w.settle_due(max_ticks=60)
    state = await w.repo.get_sync_state(U_SCOPE)
    assert dict(state.applied_versions) == {**target, **EXTRACTOR_VERSIONS}
    assert not planner.stale_versions(next(iter(w.repo.records.values())))


async def test_edit_is_not_starved_by_a_long_cascade(monkeypatch):
    """§17.1: class-0 work is never starved by class 1-3 work."""
    monkeypatch.setattr(svc_mod, "CASCADE_OPS_PER_TICK", 5)
    w = World()
    big = w.drive.add("Big", folder=True)
    for i in range(40):
        w.drive.add(f"f{i}.pdf", parent=big)
    doc = w.drive.add("contract.pdf")
    await w.settle(max_ticks=100)
    for kid in [i for i, c in w.drive.items.items() if c.parent_id == big]:
        w.drive.items.pop(kid)
    w.drive.items.pop(big)
    w.drive.log.append(replace(w.drive.log[0], item_id=big, deleted=True))   # a provider sending only the folder
    w.drive.rename(doc, "contract-signed.pdf")
    await w.tick()
    assert w.rec(doc).has_card and w.rec(doc).due_at is None   # edited card done in the first tick
    assert w.rec(big) is not None                               # while the cascade is still running
    await w.settle(max_ticks=100)
    assert w.rec(big) is None


async def test_a_budget_exhausted_tick_continues_and_the_continuation_finishes():
    """§17.5: exactly one continuation, and it runs to completion."""
    w = World(page_size=5)
    for i in range(40):
        w.drive.add(f"f{i}.pdf")
    orig = w.drive.get_changes
    calls = {"n": 0}

    async def slow_first_tick(user_id, cursor):
        calls["n"] += 1
        if calls["n"] <= 3:
            w.clock.advance(TICK_START_BUDGET.total_seconds() / 3)
        return await orig(user_id, cursor)

    w.drive.get_changes = slow_first_tick
    await w.settle()
    assert len(w.repo.records) == 40 and all(r.has_card for r in w.repo.records.values())
