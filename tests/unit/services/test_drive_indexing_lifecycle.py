"""Sync state lifecycle in the tick (USER_DRIVE_INDEXING_RFC §7.1, §7.6, §12.3): a new state needs no upgrade pass,
the gate decides on the leased state, and a regain after the grace period rebuilds from epoch 1."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index import CURRENT_VERSIONS, EpochPhase, ErrorCategory
from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.user_drive import DriveConnection, DriveThrottled

from .test_drive_indexing_service import EXTRACTOR_VERSIONS, World
from .drive_index_fakes import U_SCOPE


class TestNewStateStartsApplied:

    async def test_onboarding_reads_no_upgrade_page(self):
        w = World()
        for i in range(5):
            w.drive.add(f"f{i}.pdf")
        pages = []
        orig = w.repo.records_page

        async def counting(user_id, after, limit):
            pages.append(after)
            return await orig(user_id, after, limit)

        w.repo.records_page = counting
        await w.settle()
        st = await w.repo.get_sync_state(U_SCOPE)
        assert dict(st.applied_versions) == {**CURRENT_VERSIONS, **EXTRACTOR_VERSIONS}
        assert pages == []


class TestGateDecidesOnTheLeasedState:

    async def test_a_purge_started_between_the_read_and_the_lease_is_finished(self):
        """The gate's first decision reads the state without the lease; the decision that counts is taken again on
        the state the lease returned."""
        w = World()
        a = w.drive.add("a.pdf")
        await w.settle()
        stored = await w.repo.get_sync_state(U_SCOPE)
        await w.repo.save_sync_state(replace(stored, purging=True))
        real_get = w.repo.get_sync_state

        async def stale_read(user_id):
            st = await real_get(user_id)
            return replace(st, purging=False) if st is not None else None

        w.repo.get_sync_state = stale_read
        result = await w.tick()
        assert result.status in ("purged", "purging")
        assert w.rec(a) is None


class TestSuccessfulTickClearsLastError:

    async def test_last_error_is_cleared_by_a_clean_tick(self):
        w = World()
        w.drive.add("a.pdf")
        await w.settle()
        w.drive.raise_next = DriveThrottled(retry_after_s=5)
        await w.tick()
        assert (await w.repo.get_sync_state(U_SCOPE)).last_error is ErrorCategory.THROTTLED
        await w.tick()
        assert (await w.repo.get_sync_state(U_SCOPE)).last_error is None


class TestRegainAfterTheGracePeriod:

    async def _lost_past_ttl(self):
        w = World()
        a = w.drive.add("a.pdf")
        b = w.drive.add("b.pdf")
        await w.settle()
        assert w.carded() == {a, b}
        w.drive.connection_state = DriveConnection(connected=False)
        assert (await w.tick()).status == "access_lost"
        w.clock.advance(ACCESS_LOSS_TTL.total_seconds() + 60)
        return w, a, b

    async def test_regain_after_the_ttl_purges_then_rebuilds_from_epoch_one(self):
        w, a, b = await self._lost_past_ttl()
        w.drive.connection_state = DriveConnection(connected=True, drive_id="d1")
        embedded = len(w.emb.calls)
        await w.settle()
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.epoch == 1 and st.epoch_phase is EpochPhase.IDLE and st.initial_enumeration_done
        assert st.access_lost_at is None and not st.purging
        assert w.carded() == {a, b} and len(w.emb.calls) - embedded == 2

    async def test_purge_past_the_ttl_then_a_later_regain_rebuilds(self):
        w, a, b = await self._lost_past_ttl()
        before = len(w.queue.tasks)
        assert (await w.tick()).status == "purged"
        assert w.repo.records == {} and await w.repo.get_sync_state(U_SCOPE) is None
        assert w.queue.tasks[before:] == []            # nothing to continue while access is lost
        w.clock.advance(timedelta(days=2).total_seconds())
        w.drive.connection_state = DriveConnection(connected=True, drive_id="d1")
        await w.settle()
        st = await w.repo.get_sync_state(U_SCOPE)
        assert st.epoch == 1 and st.epoch_phase is EpochPhase.IDLE
        assert w.carded() == {a, b}
