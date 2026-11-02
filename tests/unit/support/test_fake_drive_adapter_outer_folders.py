"""FakeDriveAdapter's `outer_folders` (GOOGLE_DRIVE_RFC G8, design §1.4) and planted mimes (G1): in account-wide mode
the parentless folders of the handle's own connection, by name; [] for a drive with one area; a planted mime returned on
items and changes."""
from src.domain.drive_feed_scope import FeedScope
from tests.support.drive_handles import drive_handle
from tests.support.fake_drive_adapter import FakeDriveAdapter

A = drive_handle("usr-1", "conn-1", "cred-1", "fake_b")
B = drive_handle("usr-1", "conn-2", "cred-2", "fake_b")
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


async def test_account_mode_answers_the_parentless_folders_by_name():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    root = (await fake.get_root(A)).item_id
    fake.plant(A, root, "Springfield", folder=True)
    garden = fake.plant_parentless(A, "garden plan 2026/27", folder=True)
    hamburg = fake.plant_parentless(A, "Acme Hamburg", folder=True)
    fake.plant_parentless(A, "budget.csv", b"a,b")
    tops = await fake.outer_folders(A)
    assert [t.item_id for t in tops] == [hamburg, garden]
    assert [(t.name, t.path, t.is_folder) for t in tops] == [
        ("Acme Hamburg", "Acme Hamburg", True), ("garden plan 2026/27", "garden plan 2026\u221527", True)]
    assert ("outer_folders", "conn-1") in fake.calls


async def test_account_mode_never_answers_another_connections_folders():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    fake.plant_parentless(B, "Acme Hamburg", folder=True)
    assert await fake.outer_folders(A) == []


async def test_a_closed_or_deleted_top_is_not_answered():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    closed = fake.plant_parentless(A, "Acme Hamburg", folder=True)
    gone = fake.plant_parentless(A, "Q3", folder=True)
    fake.close(A, closed)
    await fake.delete(A, gone)
    assert await fake.outer_folders(A) == []


async def test_area_mode_answers_nothing():
    fake = FakeDriveAdapter("fake_a")
    fake.plant_parentless(A, "Acme Hamburg", folder=True)
    assert await fake.outer_folders(A) == []
    assert ("outer_folders", "conn-1") in fake.calls


async def test_a_planted_mime_is_returned_on_items_and_changes():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    root = (await fake.get_root(A)).item_id
    sheet = fake.plant(A, root, "Q3 budget", b"sheet", mime=XLSX)
    shared = fake.plant_parentless(A, "notes.md", b"# notes", mime="text/markdown")
    plain = fake.plant(A, root, "brief.docx", b"doc")
    assert (await fake.get_item(A, sheet)).mime_type == XLSX
    assert (await fake.get_change(A, sheet)).mime_type == XLSX
    assert (await fake.get_change(A, shared)).mime_type == "text/markdown"
    assert [c.mime_type for c in (await fake.get_changes(A, None)).changes if c.item_id == sheet] == [XLSX]
    assert (await fake.get_item(A, plain)).mime_type == ""
