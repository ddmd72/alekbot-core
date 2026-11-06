"""Fourth round of fixes after the review of the scope diff: the links a waiting diff keeps do not outlive a restart or
override a stored record, and the skeleton re-derivation past its scan cap is paged over ticks. Synthetic items only."""
from dataclasses import replace

from src.domain.drive_feed_scope import FeedScope
from src.services import drive_indexing_service as svc_mod

from .test_drive_scope_diff_tick import SCOPE, build, state_of
from .test_drive_scope_fix_batch import cont_tasks


async def test_the_links_of_a_waiting_diff_are_dropped_by_a_restart_and_a_stored_parent_wins(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd])
    await w.settle()
    await w.scope(folders=[w.rd, w.coimbra, w.acme])
    # a waiting diff left the old parent of Acme (the drive root) in the state
    state = await state_of(w)
    stale = f"{w.acme}\x1f{w.coimbra}"
    await w.repo.save_sync_state(replace(state, diff_parents=(stale,)))
    await w.scope(whole=True)                                    # the scope now covers everything: a restart
    await w.svc.tick(SCOPE)
    assert (await state_of(w)).diff_parents == ()
    await w.settle()
    # Acme sits under Projects; the restart's enumeration stored it, and a later narrowing is judged by that record
    await w.scope(folders=[w.rd, w.acme])
    await w.settle()
    names = w.names()
    assert {"Projects", "Acme", "Acme work.docx"} <= set(names) and "Coimbra 2024" not in names
    assert names["Acme"].parent_id == w.projects


async def test_a_stored_record_is_not_overridden_by_a_kept_link():
    w = await build(FeedScope.ACCOUNT)
    await w.scope(folders=[w.rd, w.acme, w.coimbra])
    await w.settle()
    state = await state_of(w)
    # the kept link says Acme sits inside Coimbra, which stays; the record says it sits in Projects
    await w.repo.save_sync_state(replace(state, diff_parents=(f"{w.acme}\x1f{w.coimbra}",)))
    await w.scope(folders=[w.rd, w.coimbra])                     # Acme leaves
    await w.settle()
    names = w.names()
    assert "Acme" not in names and "Acme work.docx" not in names and "Coimbra notes.txt" in names


async def test_skeletons_past_the_scan_cap_are_derived_over_ticks_each_page_once(monkeypatch):
    w = await build(FeedScope.ACCOUNT)
    parent, chain = w.root, []
    for i in range(8):
        parent = w.plant(parent, f"Level {i}", True)
        chain.append(parent)
    await w.scope(folders=[chain[-1]])
    await w.settle()
    state = await state_of(w)
    await w.repo.save_sync_state(replace(state, ancestry_dirty=True))
    monkeypatch.setattr(svc_mod, "_SKELETON_SCAN", 2)
    monkeypatch.setattr(svc_mod, "_SKELETON_PAGES", 1)
    monkeypatch.setattr(svc_mod, "RECORD_WRITE_BATCH", 4)
    after_values = []
    original = w.repo.records_page

    async def counting(scope, after, limit):
        after_values.append(after)
        return await original(scope, after, limit)

    w.repo.records_page = counting
    before = cont_tasks(w)
    ticks = 0
    for _ in range(30):
        await w.svc.tick(SCOPE)
        w.clock.advance(60)
        ticks += 1
        if not (await state_of(w)).ancestry_dirty:
            break
        assert cont_tasks(w) == before                           # a tick that only scanned asks for no continuation
    state = await state_of(w)
    assert not state.ancestry_dirty and state.skeleton_cursor == ""
    assert ticks > 2                                             # more than one page of the index
    assert len(after_values) == len(set(after_values))           # each page read once
