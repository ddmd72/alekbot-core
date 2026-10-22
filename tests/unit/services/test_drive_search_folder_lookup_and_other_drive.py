"""Two answers that must not overclaim (USER_DRIVE_INDEXING_RFC §10.1 step 0, §10.2):
- a folder not found while the index is still being built may simply not be indexed yet: the building line comes
  first;
- a different drive connected now is not "not connected": the user need not connect anything."""
from dataclasses import replace

from src.domain.drive_index import EpochPhase
from src.domain.drive_search import STATUS_DISCONNECTED, STATUS_FOLDER_NOT_FOUND, render_results
from src.domain.user_drive import DriveConnection

from .test_drive_search_service import World

BUILDING = "The index is still being built"


async def _settled():
    w = World()
    w.drive.add("lease.pdf")
    await w.settle()
    return w


async def test_folder_not_found_while_building_says_building_first():
    w = await _settled()
    state = await w.repo.get_sync_state("u")
    await w.repo.save_sync_state(replace(state, epoch_phase=EpochPhase.ENUMERATING))
    result = await w.search("lease", folder="Taxes/2025")
    text = render_results(result)
    assert result.status == STATUS_FOLDER_NOT_FOUND and result.envelope.building
    assert text.startswith(BUILDING) and "No folder matching 'Taxes/2025'" in text


async def test_folder_not_found_on_a_settled_index_is_unchanged():
    w = await _settled()
    text = render_results(await w.search("lease", folder="Taxes/2025"))
    assert text.startswith("No folder matching 'Taxes/2025'") and BUILDING not in text


async def test_a_different_drive_is_not_called_not_connected():
    w = await _settled()
    w.drive.connection_state = DriveConnection(connected=True, drive_id="d2")
    result = await w.search("lease")
    text = render_results(result)
    assert result.status == STATUS_DISCONNECTED and result.other_drive
    assert "different drive" in text and "not connected" not in text and text.endswith("Browse it.")


async def test_a_missing_connection_still_says_not_connected():
    w = await _settled()
    w.drive.connection_state = DriveConnection(connected=False)
    result = await w.search("lease")
    text = render_results(result)
    assert not result.other_drive and "not connected" in text and "different drive" not in text
