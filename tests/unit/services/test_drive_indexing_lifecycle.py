"""Sync state lifecycle in the tick (USER_DRIVE_INDEXING_RFC §7.1, §7.6, §12.3): a new state needs no upgrade pass,
the gate decides on the leased state, and a regain after the grace period rebuilds from epoch 1."""
from dataclasses import replace
from datetime import timedelta

from src.domain.drive_index import CURRENT_VERSIONS, EpochPhase, ErrorCategory
from src.domain.drive_index_params import ACCESS_LOSS_TTL
from src.domain.user_drive import DriveConnection, DriveThrottled

from .test_drive_indexing_service import World


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
        st = await w.repo.get_sync_state("u")
        assert dict(st.applied_versions) == dict(CURRENT_VERSIONS)
        assert pages == []


class TestGateDecidesOnTheLeasedState:

    async def test_a_purge_started_between_the_read_and_the_lease_is_finished(self):
        """The gate's first decision reads the state without the lease; the decision that counts is taken again on
        the state the lease returned."""
        w = World()
        a = w.drive.add("a.pdf")
        await w.settle()
        stored = await w.repo.get_sync_state("u")
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
        assert (await w.repo.get_sync_state("u")).last_error is ErrorCategory.THROTTLED
        await w.tick()
        assert (await w.repo.get_sync_state("u")).last_error is None
