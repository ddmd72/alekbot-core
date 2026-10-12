"""Pins RFC-required tick behaviour that no other test pinned (retro_tick.md F8 c-j).

Each test names the surviving mutation of retro_tick_mutations.txt it kills. The code was already right;
these tests keep it right.
"""
from src.domain import drive_card
from src.domain import drive_index_planner as planner
from src.domain.drive_card import card_text
from src.domain.drive_index import DriveChanges, EpochPhase, ErrorCategory, Reason
from src.domain.drive_index_params import MAX_ATTEMPTS, RETRY_CAP, TICK_START_BUDGET
from src.domain.drive_index_planner import PlanContext, plan_observation
from src.domain.user_drive import DriveAccessDenied, DriveResyncRequired, DriveThrottled
from src.services import drive_indexing_service as svc_mod

from .drive_index_fakes import ROOT, FakeEmbeddings
from .test_drive_indexing_service import World


class _Runaway(BaseException):
    """Escapes the tick's catch-all, so a loop that never ends fails the test instead of hanging it."""


def _conts(w, since=0):
    return [t for t in w.queue.tasks[since:] if ":cont:" in (t["key"] or "")]


def _bump_card_version(monkeypatch):
    monkeypatch.setattr(drive_card, "CARD_VERSION", 2)
    monkeypatch.setattr(planner, "CARD_VERSION", 2)
    monkeypatch.setattr(svc_mod, "CURRENT_VERSIONS", {"policy": 1, "card": 2, "geo_table": 1})


class TestRelistingRetryBudget:
    async def test_relisting_is_dropped_after_max_attempts(self):
        """(c) M35 — §8.2, plan Δ18: a re-listing that keeps failing is dropped, never retried forever."""
        w = World()
        hidden = w.drive.add(".hidden", folder=True)
        w.drive.add("a.pdf", parent=hidden)
        await w.settle()
        w.drive.rename(hidden, "visible")
        orig = w.drive.list_children_changes
        calls = {"n": 0}

        async def denied(user_id, folder_id, limit, cursor=None):
            if folder_id == hidden:
                calls["n"] += 1
                raise DriveAccessDenied("refused")
            return await orig(user_id, folder_id, limit, cursor)

        w.drive.list_children_changes = denied
        for _ in range(MAX_ATTEMPTS + 3):
            await w.settle()
            w.clock.advance(RETRY_CAP.total_seconds())   # every backoff has elapsed
        folder = w.rec(hidden)
        assert folder.reobserve_pending is False and folder.reobserve_cursor is None
        assert calls["n"] == MAX_ATTEMPTS


class TestVersionUpgradeVerification:
    async def test_versions_are_applied_only_after_a_clean_verification_pass(self, monkeypatch):
        """(d) M29 — §12.3: a pass that found stale records schedules work; it does not apply the versions."""
        w = World()
        for i in range(3):
            w.drive.add(f"f{i}.pdf")
        await w.settle()
        _bump_card_version(monkeypatch)
        result = await w.tick()
        assert result.summary.get("upgraded") == 3
        assert dict((await w.repo.get_sync_state("u")).applied_versions)["card"] == 1
        await w.settle()
        assert dict((await w.repo.get_sync_state("u")).applied_versions)["card"] == 2


class TestMaintenanceShare:
    async def test_maintenance_class_stops_at_its_share_of_the_tick(self, monkeypatch):
        """(e) M22 — §7.8: classes 3-4 get MAINTENANCE_SHARE of the tick budget, not the whole of it."""
        w = World(page_size=1000)
        for i in range(250):
            w.drive.add(f"f{i:03d}.pdf")
        await w.settle()
        _bump_card_version(monkeypatch)
        await w.tick()   # the upgrade pass schedules 250 class-3 re-embeddings
        due = await w.repo.due_records("u", w.clock.now, 1000)
        assert len(due) == 250 and all(r.priority == 3 for r in due)
        orig = w.emb.get_embedding

        async def slow(text, task_type="RETRIEVAL_DOCUMENT"):
            w.clock.advance(1.5)   # 100 embeddings = 150 s: over the 144 s share, well inside the 480 s budget
            return await orig(text, task_type)

        w.emb.get_embedding = slow
        w.emb.calls.clear()
        await w.tick()
        assert len(w.emb.calls) == 100   # one class-3 page, then the share is spent
        assert len(_conts(w)) >= 1


class TestCardEviction:
    async def test_an_equal_rank_record_never_evicts_a_card(self, monkeypatch):
        """(f) M19 — §5.4: eviction only by a strictly higher-ranked record; an equal one is capped itself."""
        w = World()
        monkeypatch.setattr(svc_mod, "MAX_CARD_RECORDS_PER_USER", 3)
        codes = [w.drive.add(f"c{i}.py") for i in range(3)]
        await w.settle()
        newcomer = w.drive.add("c9.py")
        await w.settle()
        assert w.carded() == set(codes)
        assert w.rec(newcomer).reason is Reason.USER_CARD_CAP
        assert all(w.rec(c).reason is Reason.KIND_NOT_ADMITTED for c in codes)


class TestBookkeeping:
    """(g) The fields the fan-out and the staleness alert read (§12.1, Part D)."""

    async def test_success_resets_systemic_failures(self):
        """M15: a clean tick ends the systemic backoff."""
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        w.drive.raise_next = DriveThrottled(retry_after_s=30)
        assert (await w.tick()).status == "throttled"
        assert (await w.repo.get_sync_state("u")).systemic_failures == 1
        w.clock.advance(30)
        assert (await w.tick()).status == "done"
        assert (await w.repo.get_sync_state("u")).systemic_failures == 0

    def _stalled_tick_world(self):
        """A settled drive whose next change page eats the whole tick budget and carries nothing."""
        w = World()
        w.drive.add("a.pdf")
        return w

    async def _stall(self, w):
        state = await w.repo.get_sync_state("u")

        async def stalled(user_id, cursor):
            w.clock.advance(TICK_START_BUDGET.total_seconds())
            return DriveChanges((), cursor, False, False, "d1", ROOT)

        w.drive.get_changes = stalled
        return state

    async def test_last_success_moves_only_on_progress(self):
        """M13 — §12.1: a tick that continues without doing anything is not a success."""
        w = self._stalled_tick_world()
        await w.settle()
        w.clock.advance(600)
        before = await self._stall(w)
        result = await w.tick()
        assert result.status == "done" and len(_conts(w)) >= 1
        after = await w.repo.get_sync_state("u")
        assert before.last_success_at is not None
        assert after.last_success_at == before.last_success_at

    async def test_continuation_marker_is_the_tick_start(self):
        """M39 — Part D: the fan-out skips a user whose continuation is in flight, read from this marker."""
        w = self._stalled_tick_world()
        await w.settle()
        w.clock.advance(600)
        tick_now = w.clock.now
        await self._stall(w)
        before = len(w.queue.tasks)
        await w.tick()
        assert len(_conts(w, before)) == 1
        assert (await w.repo.get_sync_state("u")).continuation_enqueued_at == tick_now

    async def test_observed_as_of_is_the_round_start(self):
        """M30 — §12.1: a round spanning ticks is as fresh as its start, not as its last page."""
        w = World(page_size=5)
        for i in range(40):
            w.drive.add(f"f{i}.pdf")
        orig = w.drive.get_changes

        async def slow(user_id, cursor):
            w.clock.advance(TICK_START_BUDGET.total_seconds() / 3)
            return await orig(user_id, cursor)

        w.drive.get_changes = slow
        round_start = w.clock.now
        ticks = 0
        while True:
            await w.tick()
            ticks += 1
            state = await w.repo.get_sync_state("u")
            if state.round_complete:
                break
            assert ticks < 20
            w.clock.advance(1)
        assert ticks > 1
        assert state.observed_as_of == round_start


class TestSweepContinues:
    async def test_unfinished_sweep_enqueues_a_continuation(self):
        """(h) M3 — §7.5: a sweep cut by the budget continues in-chain, not on the next fan-out."""
        w = World()
        w.drive.add("a.pdf")
        b = w.drive.add("b.pdf")
        await w.settle()
        w.drive.items.pop(b)          # gone, and the log never says so
        w.drive.resync_next = True
        orig = w.repo.records_seen_before

        async def slow(user_id, epoch, limit):
            w.clock.advance(TICK_START_BUDGET.total_seconds())
            return await orig(user_id, epoch, limit)

        w.repo.records_seen_before = slow
        before = len(w.queue.tasks)
        await w.tick()
        state = await w.repo.get_sync_state("u")
        assert state.round_complete and state.epoch_phase is EpochPhase.SWEEPING
        assert len(_conts(w, before)) == 1


class TestSecondResync:
    async def test_a_second_410_in_one_tick_is_systemic(self):
        """(i) M27 — a restart cursor that expires again ends the tick instead of looping on new epochs."""
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        epoch = (await w.repo.get_sync_state("u")).epoch
        calls = {"n": 0}

        async def always_410(user_id, cursor):
            calls["n"] += 1
            if calls["n"] > 5:
                raise _Runaway()
            raise DriveResyncRequired("full:0")

        w.drive.get_changes = always_410
        result = await w.tick()
        assert result.status == "error" and calls["n"] == 2
        state = await w.repo.get_sync_state("u")
        assert state.epoch == epoch + 1 and state.last_error is ErrorCategory.PROVIDER_ERROR


class TestEmbedFreshness:
    async def test_a_card_renamed_during_its_embedding_is_not_written_with_the_old_vector(self):
        """(j) M7 — §8.5: a vector is written only over a record whose card text is still the one embedded."""
        w = World()
        fid = w.drive.add("a.pdf")
        renamed = {"done": False}
        orig = w.emb.get_embedding

        async def rename_while_embedding(text, task_type="RETRIEVAL_DOCUMENT"):
            if text.startswith("a.pdf") and not renamed["done"]:
                renamed["done"] = True
                cur = w.rec(fid)
                w.drive.rename(fid, "b.pdf")
                ctx = PlanContext(user_id="u", root_item_id=ROOT, epoch=cur.seen_epoch, phase=EpochPhase.IDLE,
                                  now=w.clock.now, place_of=lambda lat, lon: None)
                w.repo.records[("u", fid)] = plan_observation(w.drive.items[fid], cur, None, None, ctx).upsert
            return await orig(text, task_type)

        w.emb.get_embedding = rename_while_embedding
        await w.settle()
        assert renamed["done"]
        rec = w.rec(fid)
        assert rec.name == "b.pdf" and rec.has_card
        expected = await FakeEmbeddings().get_embedding(card_text(rec))
        assert w.repo.vectors[("u", fid)] == expected
