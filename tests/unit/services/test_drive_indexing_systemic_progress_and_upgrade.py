"""The tick keeps saved progress under systemic errors, classifies probe errors, treats nudged items as class 0,
and never spins an upgrade pass; plus two RFC behaviours no other test pinned (§7.1, §7.8, §8.2, §12.3)."""
from dataclasses import replace

import aiohttp
import pytest

from src.domain import drive_card
from src.domain import drive_index_planner as planner
from src.domain.drive_index_params import RETRY_CAP
from src.domain.drive_index import CARD_VERSION, CURRENT_VERSIONS, ErrorCategory, Nudge, Priority, Reason, RecordState
from src.domain.user_drive import (
    DriveAccessDenied, DriveConnection, DriveNotConnectedError, DriveReauthRequired, DriveThrottled,
)
from src.services import drive_indexing_service as svc_mod

from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import U_SCOPE


class _Runaway(BaseException):
    """Escapes the tick's catch-all, so a loop that never ends fails the test instead of hanging it."""


def _fail_on_call(w, n_fail, exc):
    """`get_changes` raises `exc` on its n-th call (counted across ticks)."""
    orig = w.drive.get_changes
    calls = {"n": 0}

    async def flaky(user_id, cursor):
        calls["n"] += 1
        if calls["n"] == n_fail:
            raise exc
        return await orig(user_id, cursor)

    w.drive.get_changes = flaky


class TestSystemicErrorKeepsObserveProgress:
    """A systemic error mid-observe must not roll back the cursor/epoch observe saved page by page."""

    @pytest.mark.parametrize("exc, status, category", [
        (DriveThrottled(retry_after_s=30), "throttled", ErrorCategory.THROTTLED),
        (DriveReauthRequired("refresh rejected"), "access_lost", ErrorCategory.RECONNECT_NEEDED),
        (ValueError("graph 5xx"), "error", ErrorCategory.PROVIDER_ERROR),
    ])
    async def test_error_on_page_three_keeps_page_progress(self, exc, status, category):
        w = World(page_size=5)
        for i in range(40):
            w.drive.add(f"f{i}.pdf")
        _fail_on_call(w, 3, exc)
        assert (await w.tick()).status == status
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.cursor == "full:10", st.cursor
        assert st.epoch == 1
        assert st.last_error is category

    async def test_throttle_still_counts_the_failure_and_marks_the_continuation(self):
        w = World(page_size=5)
        for i in range(40):
            w.drive.add(f"f{i}.pdf")
        _fail_on_call(w, 3, DriveThrottled(retry_after_s=30))
        await w.tick()
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.systemic_failures == 1 and st.continuation_enqueued_at == w.clock.now

    async def test_onboarding_liveness_under_periodic_throttling(self):
        """Graph throttles every 4th page call: onboarding still finishes."""
        w = World(page_size=5)
        for i in range(60):
            w.drive.add(f"f{i}.pdf")
        orig = w.drive.get_changes
        calls = {"n": 0}

        async def throttle_fourth(user_id, cursor):
            calls["n"] += 1
            if calls["n"] % 4 == 0:
                raise DriveThrottled(retry_after_s=1)
            return await orig(user_id, cursor)

        w.drive.get_changes = throttle_fourth
        for _ in range(40):
            await w.tick()
            w.clock.advance(5)
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.initial_enumeration_done, (st.cursor, st.epoch_phase)


    async def test_throttle_right_after_a_410_keeps_the_new_epoch(self):
        """The 410 restart is saved at once; a throttle on the next page call must not write back
        the pre-410 cursor and epoch (the next tick would take another 410 and open epoch E+2)."""
        w = World(page_size=5)
        for i in range(20):
            w.drive.add(f"f{i}.pdf")
        await w.settle()
        epoch = (await w.repo.get_sync_state(U_SCOPE)).epoch
        w.drive.resync_next = True
        orig = w.drive.get_changes
        calls = {"n": 0}

        async def throttle_after_410(user_id, cursor):
            calls["n"] += 1
            if calls["n"] == 2:
                raise DriveThrottled(retry_after_s=30)
            return await orig(user_id, cursor)

        w.drive.get_changes = throttle_after_410
        assert (await w.tick()).status == "throttled"
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.epoch == epoch + 1
        assert st.cursor == "full:0", st.cursor
        assert st.last_error is ErrorCategory.THROTTLED


def _photos_world():
    """A settled folder of 10 that grows to 610 and is renamed: the feed re-emits it and a probe is needed."""
    w = World(page_size=1000)
    top = w.drive.add("Photos", folder=True)
    for i in range(10):
        w.drive.add(f"p{i}.jpg", parent=top)
    return w, top


class TestSystemicErrorInsideInlineProbe:
    """The inline density probe's errors are classified, not charged to the item."""

    @pytest.mark.slow
    async def test_disconnect_during_inline_probe_does_not_lose_the_observation(self):
        w, top = _photos_world()
        await w.settle()
        for i in range(10, 610):
            w.drive.add(f"p{i}.jpg", parent=top)
        w.drive.rename(top, "Holiday")
        orig = w.drive.list_children_changes
        fired = {"n": 0}

        async def gone_once(user_id, folder_id, limit, cursor=None):
            if not fired["n"]:
                fired["n"] = 1
                raise DriveNotConnectedError("credentials removed mid-tick")
            return await orig(user_id, folder_id, limit, cursor)

        w.drive.list_children_changes = gone_once
        first = await w.tick()
        assert first.status == "error" and "skipped" not in first.summary
        assert w.rec(top).name == "Photos"   # the page was not applied: its cursor did not move
        w.clock.advance(3600)
        await w.settle(max_ticks=100)
        assert w.rec(top).name == "Holiday"
        assert w.rec(top).child_count == 610

    @pytest.mark.slow
    async def test_throttle_during_inline_probe_ends_the_tick(self):
        w, top = _photos_world()
        await w.settle()
        for i in range(10, 610):
            w.drive.add(f"p{i}.jpg", parent=top)
        w.drive.rename(top, "Holiday")

        async def throttled(user_id, folder_id, limit, cursor=None):
            raise DriveThrottled(retry_after_s=30)

        w.drive.list_children_changes = throttled
        res = await w.tick()
        assert res.status == "throttled" and "skipped" not in res.summary

    @pytest.mark.slow
    async def test_transport_error_during_inline_probe_leaves_the_folder_probe_pending(self):
        w, top = _photos_world()
        await w.settle()
        for i in range(10, 610):
            w.drive.add(f"p{i}.jpg", parent=top)
        w.drive.rename(top, "Holiday")

        async def disconnected(user_id, folder_id, limit, cursor=None):
            raise aiohttp.ServerDisconnectedError()

        w.drive.list_children_changes = disconnected
        res = await w.tick()
        assert "skipped" not in res.summary
        rec = w.rec(top)
        assert rec.name == "Holiday" and rec.probe_pending


class TestNudgedItemsAreClassZero:
    """Items named in a nudge are class 0 (§7.8), even when first seen."""

    async def test_nudged_new_file_is_class_zero(self):
        w = World()
        await w.settle()
        item = w.drive.add("note.md")
        w.drive.log.pop()   # delta has not caught up yet

        async def no_work(user_id, state, now, budget, summary):
            return state

        w.svc._work_off = no_work
        await w.tick(Nudge("m2", (item,)))
        assert w.rec(item).priority == int(Priority.EDIT)

    async def test_file_first_seen_through_delta_stays_class_two(self):
        w = World()
        await w.settle()
        item = w.drive.add("note.md")

        async def no_work(user_id, state, now, budget, summary):
            return state

        w.svc._work_off = no_work
        await w.tick()
        assert w.rec(item).priority == int(Priority.FIRST_SEEN)


class TestUpgradePassDoesNotSpin:
    """A pass that restarts because its stale records cannot be worked yet must not chain zero-delay ticks."""

    async def test_card_bump_with_a_backed_off_stale_record_does_not_spin(self, monkeypatch):
        """A reopened repository's re-listing keeps failing, so the folder backs off (due later) while it still
        carries a card of the old CARD_VERSION. Every pass finds it stale and restarts, and nothing is due now:
        no continuation may follow until its backoff ends."""
        w = World()
        site = w.drive.add("site", folder=True)
        for i in range(3):
            w.drive.add(f"a{i}.py", parent=site)
        git = w.drive.add(".git", parent=site, folder=True)
        w.drive.add("doc.pdf")
        await w.settle()
        assert site in w.carded()

        async def denied(user_id, folder_id, limit, cursor=None):
            raise DriveAccessDenied("listing refused")

        w.drive.list_children_changes = denied
        w.drive.delete(git)
        await w.settle()
        assert w.rec(site).reobserve_pending and w.rec(site).due_at > w.clock.now
        monkeypatch.setattr(drive_card, "CARD_VERSION", CARD_VERSION + 1)
        monkeypatch.setattr(planner, "CARD_VERSION", CARD_VERSION + 1)
        monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {**CURRENT_VERSIONS, "card": CARD_VERSION + 1})
        zero_delay_chain = 0
        longest = 0
        for _ in range(30):
            before = len(w.queue.tasks)
            await w.tick()
            conts = [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
            if not conts:
                break
            delay = max(t["delay"] for t in conts)
            zero_delay_chain = zero_delay_chain + 1 if delay == 0 else 0
            longest = max(longest, zero_delay_chain)
            w.clock.advance(max(1, delay))
        else:
            pytest.fail("the continuation chain never ended")
        assert longest < 5, longest

    async def test_only_a_pass_mid_way_continues_on_versions_alone(self, monkeypatch):
        """With nothing due, a version difference continues the tick only while a pass is mid-way."""
        w = World()
        for i in range(5):
            w.drive.add(f"f{i}.pdf")
        await w.settle()
        monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {"policy": 1, "card": 1, "geo_table": 2})
        st = await w.repo.get_sync_state(U_SCOPE)
        assert await w.svc._has_more("u", replace(st, upgrade_cursor="i00003"), w.clock.now)
        assert not await w.svc._has_more("u", replace(st, upgrade_cursor=None), w.clock.now)

    async def test_a_pass_that_wrote_continues_to_verify_in_chain(self, monkeypatch):
        """A pass that ended this tick having written (re-stamped or scheduled) continues the
        chain, so the verification pass runs in-chain; one that wrote nothing waits."""
        w = World()
        for i in range(5):
            w.drive.add(f"f{i}.pdf")
        await w.settle()
        monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {"policy": 1, "card": 1, "geo_table": 2})
        st = replace(await w.repo.get_sync_state(U_SCOPE), upgrade_cursor=None)
        assert await w.svc._has_more("u", st, w.clock.now, upgrade_wrote=True)
        assert not await w.svc._has_more("u", st, w.clock.now, upgrade_wrote=False)


class TestRfcBehaviourPinned:
    """Required behaviour that no other test pinned."""

    async def test_page_from_another_drive_purges_and_onboards(self):
        """§7.1 step 1: credentials carry no drive id, so only the page's drive_id reveals the switch."""
        w = World()
        a = w.drive.add("a.pdf")
        await w.settle()
        w.drive.connection_state = DriveConnection(connected=True, drive_id="")
        orig = w.drive.get_changes

        async def other_drive(user_id, cursor):
            return replace(await orig(user_id, cursor), drive_id="d2")

        w.drive.get_changes = other_drive
        res = await w.tick()
        assert res.status == "purged" and w.repo.records == {}
        await w.settle()
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.drive_id == "d2" and w.rec(a) is not None

    @pytest.mark.slow
    async def test_probe_budget_spent_children_held_unresolved_then_resolved(self, monkeypatch):
        """§3.6: one item call per tick for two dense folders. The second waits probe_pending and its children
        are held unresolved, never looped over; both end DENSE_TREE with no child records."""
        monkeypatch.setattr(svc_mod, "ITEM_CALLS_PER_TICK", 1)
        w = World(page_size=2000)
        big = [w.drive.add(f"big{j}", folder=True) for j in range(2)]
        for j, b in enumerate(big):
            for i in range(510):
                w.drive.add(f"p{j}_{i}.bin", parent=b)
        orig_due = w.repo.due_records
        calls = {"n": 0}

        async def bounded_due(user_id, now, limit):
            calls["n"] += 1
            if calls["n"] > 500:   # the real path needs a few dozen at most
                raise _Runaway("due loop never ended")
            return await orig_due(user_id, now, limit)

        w.repo.due_records = bounded_due
        await w.tick()
        held = [r for r in w.repo.records.values() if r.parent_id in big and r.state is RecordState.UNRESOLVED]
        pending = [b for b in big if w.rec(b).probe_pending]
        assert pending, "the probe budget was not spent on the first tick"
        assert held and all(r.due_at is None for r in held if r.parent_id in pending)
        await w.settle(max_ticks=100)
        assert all(w.rec(b).reason is Reason.DENSE_TREE for b in big)
        assert sum(1 for r in w.repo.records.values() if r.parent_id in big) == 0


def _bumped_card(monkeypatch):
    """CARD_VERSION + 1; returns the map the service applies once clean (with the extractor keys, CORE-DELTA 5)."""
    monkeypatch.setattr(drive_card, "CARD_VERSION", CARD_VERSION + 1)
    monkeypatch.setattr(planner, "CARD_VERSION", CARD_VERSION + 1)
    target = {**CURRENT_VERSIONS, "card": CARD_VERSION + 1}
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", target)
    return {**target, **EXTRACTOR_VERSIONS}


async def _run_chain(w, max_ticks):
    """Ticks while each one enqueues a continuation (delays elapse). Returns (ticks run, longest run of
    back-to-back zero-delay continuations)."""
    ticks = zero_delay_chain = longest = 0
    for _ in range(max_ticks):
        before = len(w.queue.tasks)
        await w.tick()
        ticks += 1
        conts = [t for t in w.queue.tasks[before:] if ":cont:" in (t["key"] or "")]
        if not conts:
            break
        delay = max(t["delay"] for t in conts)
        zero_delay_chain = zero_delay_chain + 1 if delay == 0 else 0
        longest = max(longest, zero_delay_chain)
        w.clock.advance(max(1, delay))
    return ticks, longest


async def _converge_with_fanout(w, target, rounds=10):
    """The periodic fan-out resumes a quiet user: let backoffs elapse and run chains until the bump applies.
    Returns the longest zero-delay run seen in any chain."""
    longest = 0
    for _ in range(rounds):
        _, chain = await _run_chain(w, 100)
        longest = max(longest, chain)
        if dict((await w.repo.get_sync_state(U_SCOPE)).applied_versions) == target:
            return longest
        w.clock.advance(RETRY_CAP.total_seconds())
    pytest.fail("the version bump never applied")


class TestUpgradePassSkipsParkedRecords:
    """A record parked under an unresolved parent (UNRESOLVED, due_at None) is
    not re-scheduled by the upgrade pass; the parent's rescope re-plans it. Scheduling it only re-parked it,
    a zero-delay spin."""

    @staticmethod
    async def _photos_grown_past_dense_with_probe_refused(w):
        f = w.drive.add("Photos", folder=True)
        for i in range(10):
            w.drive.add(f"p{i}.pdf", parent=f)
        await w.settle()
        for i in range(10, 610):
            w.drive.add(f"p{i}.pdf", parent=f)
        w.drive.reemit(f)
        orig = w.drive.list_children_changes

        async def denied(user_id, folder_id, limit, cursor=None):
            raise DriveAccessDenied("probe refused")

        w.drive.list_children_changes = denied
        return f, orig

    @pytest.mark.slow
    async def test_card_bump_with_a_deferred_reprobe_does_not_spin(self, monkeypatch):
        w = World(page_size=2000)
        f, orig = await self._photos_grown_past_dense_with_probe_refused(w)
        target = _bumped_card(monkeypatch)
        ticks, _ = await _run_chain(w, 30)
        assert ticks < 30, ticks
        parked = [r for r in w.repo.records.values()
                  if r.parent_id == f and r.state is RecordState.UNRESOLVED and r.due_at is None]
        # the scenario: parked under the waiting folder; parking dropped their old cards, so nothing of them is
        # stale and the pass has nothing to re-schedule into a re-park
        assert parked and not any(r.has_card or r.card_hash or planner.stale_versions(r) for r in parked)
        w.drive.list_children_changes = orig
        await _converge_with_fanout(w, target)
        assert not [r for r in w.repo.records.values() if planner.stale_versions(r)]
        assert w.rec(f).state is not RecordState.UNRESOLVED
        assert not [r for r in w.repo.records.values() if r.parent_id == f and r.state is RecordState.UNRESOLVED]

    @pytest.mark.slow
    async def test_not_dense_outcome_converges_without_a_spin(self, monkeypatch):
        """The probe keeps failing: after its attempts the folder is classified not dense and every child gets
        its card rebuilt, with no zero-delay chain on the way."""
        w = World(page_size=2000)
        f, _ = await self._photos_grown_past_dense_with_probe_refused(w)
        target = _bumped_card(monkeypatch)
        longest = await _converge_with_fanout(w, target)
        assert longest < 30, longest
        assert not [r for r in w.repo.records.values() if planner.stale_versions(r)]
        assert sum(1 for r in w.repo.records.values() if r.parent_id == f and r.has_card) == 610
