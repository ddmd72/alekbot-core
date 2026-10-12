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
