"""OneDrive never defers a change key (GOOGLE_DRIVE_RFC GE-36): a delta item's cTag is final, so its feed has no pending
change and `resolve_content_versions` returns what it was given, in order, with no Graph call."""
from src.domain.drive_index import DriveChange
from tests.unit.adapters import test_onedrive_drive_contract as _contract

world = _contract.world        # the two-account world fixture


async def test_the_feed_has_no_pending_change_and_resolving_it_calls_nothing(world):
    h = world.handle
    await world.port.upload(h, (await world.port.get_root(h)).item_id, "f-1.txt", b"one", "text/plain")
    page = await world.port.get_changes(h, None)
    assert page.changes and not any(c.content_version_pending for c in page.changes)
    given = [*page.changes, DriveChange(item_id="f-9", deleted=True)]
    before = len(world.fake.requests)
    assert await world.port.resolve_content_versions(h, given) == given
    assert len(world.fake.requests) == before
