"""G4 (GOOGLE_DRIVE_RFC §5, design §1.3, Δ3): a "/" inside a provider name prints as DIVISION SLASH in a path segment,
the folder match reads the printed segment, and the fake drive prints paths as a real adapter does. G11's flag on
ListOutcome defaults off."""
import pytest

from src.domain.drive_feed_scope import FeedScope
from src.domain.user_drive import (
    NAME_SLASH, DriveItem, ListOutcome, join_drive_path, match_child_folder, path_segment,
)
from tests.support.drive_handles import drive_handle
from tests.support.fake_drive_adapter import FakeDriveAdapter

GARDEN = "Garden plan 2026/27"
GARDEN_PRINTED = "Garden plan 2026∕27"
HANDLE = drive_handle("usr-1", "conn-1", "cred-1", "fake_b")


def _folder(item_id: str, name: str) -> DriveItem:
    return DriveItem(item_id=item_id, name=name, path=name, is_folder=True)


def test_name_slash_is_the_division_slash():
    assert NAME_SLASH == "∕"


def test_a_slash_in_a_name_prints_as_the_division_slash():
    assert path_segment(GARDEN) == GARDEN_PRINTED


@pytest.mark.parametrize("name", ["Springfield", "Acme Hamburg", "Clients: 2026", "Plans: 2026?", "notes.md", ""])
def test_other_names_are_unchanged(name):
    assert path_segment(name) == name


def test_join_escapes_the_name_never_the_parent():
    assert join_drive_path("Springfield", GARDEN) == f"Springfield/{GARDEN_PRINTED}"
    assert join_drive_path("Acme Hamburg/Q3", "Q3 budget") == "Acme Hamburg/Q3/Q3 budget"


@pytest.mark.parametrize("name,printed", [(GARDEN, GARDEN_PRINTED), ("Springfield", "Springfield")])
def test_join_without_a_parent_is_the_segment(name, printed):
    assert join_drive_path("", name) == printed


def test_match_finds_the_child_by_its_printed_segment():
    child = _folder("fid-1", GARDEN)
    assert match_child_folder([child], GARDEN_PRINTED) is child


def test_match_by_key_reads_the_printed_segment_too():
    child = _folder("fid-1", GARDEN)
    assert match_child_folder([child], GARDEN_PRINTED.upper()) is child


def test_an_exact_match_comes_before_a_key_match():
    loose, exact = _folder("fid-1", "garden plan 2026/27"), _folder("fid-2", GARDEN)
    assert match_child_folder([loose, exact], GARDEN_PRINTED) is exact


def test_a_file_never_matches():
    file = DriveItem(item_id="fid-1", name=GARDEN, path=GARDEN, is_folder=False)
    assert match_child_folder([file], GARDEN_PRINTED) is None


async def test_the_fake_prints_the_escaped_path_and_keeps_the_name_exact():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    root = (await fake.get_root(HANDLE)).item_id
    springfield = fake.plant(HANDLE, root, "Springfield", folder=True)
    garden = fake.plant(HANDLE, springfield, GARDEN, folder=True)
    budget = fake.plant(HANDLE, garden, "budget.csv", b"a,b")
    item = await fake.get_item(HANDLE, garden)
    assert item.name == GARDEN and item.path == f"Springfield/{GARDEN_PRINTED}"
    assert (await fake.get_item(HANDLE, budget)).path == f"Springfield/{GARDEN_PRINTED}/budget.csv"


async def test_a_parentless_name_prints_escaped_too():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    top = fake.plant_parentless(HANDLE, GARDEN, folder=True)
    item = await fake.get_item(HANDLE, top)
    assert (item.name, item.path) == (GARDEN, GARDEN_PRINTED)


def test_list_outcome_scope_tops_defaults_off():
    folder = _folder("fid-1", "Springfield")
    assert ListOutcome(folder=folder, items=[], truncated=False).scope_tops is False
    assert ListOutcome(folder=folder, items=[], truncated=False, scope_tops=True).scope_tops is True
