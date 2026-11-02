"""Slice 2a integration: a move to a drive's top level is refused (never a silent no-op); the deleted-files search
passes its already canonical paths through unchanged. Technical ids and the invented cast only."""
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_deleted_result import DeletedSearchResult
from src.domain.drive_mounts import NO_TOP_LEVEL_DESTINATION_LINE
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.user_drive import DriveItem
from tests.unit.infrastructure.test_drive_operations_mounts import CTX, PLAN, world  # noqa: F401


@pytest.mark.parametrize("destination", ["Drive", "Acme", "drive", "/"])
async def test_a_move_to_a_bare_mount_is_a_request_error_and_nothing_moves(world, destination):  # noqa: F811
    out = await world.ops.move(CTX, "usr-1", f"drive:a1:{world.plan}", folder=destination, wait_s=5)
    assert out.text == NO_TOP_LEVEL_DESTINATION_LINE and out.cls is OutcomeClass.REQUEST_ERROR and not out.success
    assert world.drive.tree(world.one.handle).nodes[world.plan].parent_id == world.rd


async def test_a_move_into_a_folder_of_the_primary_still_works(world):  # noqa: F811
    out = await world.ops.move(CTX, "usr-1", f"drive:a1:{world.plan}", folder="Drive/R&D", wait_s=5)
    assert out.success and f"Drive/R&D/{PLAN}" in out.text


async def test_search_deleted_paths_are_not_prefixed_again(world):  # noqa: F811
    item = DriveItem(item_id="m1", name="meeting.md", path="Drive/_Transcripts/Archive/meeting.md", is_folder=False)
    world.search.search_deleted = AsyncMock(return_value=DeletedSearchResult("Deleted files (1):", [item], ref_tag="a1"))
    out = await world.ops.search_deleted(CTX, "usr-1", "meeting")
    assert [i.path for i in out.items] == ["Drive/_Transcripts/Archive/meeting.md"]
