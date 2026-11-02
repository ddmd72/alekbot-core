"""T-3 / T-7 for slice 2b (MULTI_DRIVE_RFC §11.3, design §1.3, §1.4): `FakeDriveAdapter` in account-wide mode (`fake_b`,
`FeedScope.ACCOUNT`) and a fake that declares `writable=False` pass the whole contract suite (each declaring its values
for the capabilities case), and the account-wide mode's own behaviour: a parentless shared item and a planted
`ItemAccess`."""
import pytest

from src.domain.drive_feed_scope import FeedScope
from src.domain.drive_item_access import ItemAccess
from tests.contracts.user_drive_contract import UserDriveContract
from tests.support.drive_handles import drive_handle
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.support.test_fake_drive_adapter_contract import _FakeWorld


class _AccountWorld(_FakeWorld):
    def __init__(self, *, writable: bool = True, feed_scope: FeedScope = FeedScope.ACCOUNT) -> None:
        super().__init__(255)
        self.id_space = "fake_b"
        self.fake = FakeDriveAdapter("fake_b", max_path_chars=255, writable=writable, feed_scope=feed_scope)
        self.port = self.fake
        self.handle = drive_handle("usr-1", "conn-1", "cred-1", "fake_b")
        self.other = drive_handle("usr-1", "conn-2", "cred-2", "fake_b")
        self.drive_writable = writable
        self.feed_scope = feed_scope


@pytest.fixture(params=["account", "not-writable"])
def world(request):
    if request.param == "account":
        return _AccountWorld()
    return _AccountWorld(writable=False, feed_scope=FeedScope.AREA)


class TestFakeDriveAdapterAccountContract(UserDriveContract):
    pass


HANDLE = drive_handle("usr-1", "conn-1", "cred-1", "fake_b")


async def test_the_account_root_has_no_area_path():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    assert await fake.area_path(HANDLE) == "/"
    assert await FakeDriveAdapter("fake_a").area_path(HANDLE) == "/Fake"


async def test_a_parentless_item():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    root = (await fake.get_root(HANDLE)).item_id
    shared = fake.plant_parentless(HANDLE, "lab-report.pdf", b"LAB-2025-03-0417")
    item = await fake.get_item(HANDLE, shared)
    assert item.path == "lab-report.pdf" and not item.is_folder
    assert (await fake.get_change(HANDLE, shared)).parent_id == ""
    feed = await fake.get_changes(HANDLE, None)
    assert [c.parent_id for c in feed.changes if c.item_id == shared] == [""]
    assert shared not in {c.item_id for c in await fake.list_children(HANDLE, root)}


async def test_a_parentless_folder():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    folder = fake.plant_parentless(HANDLE, "Coimbra 2024", folder=True)
    assert (await fake.get_item(HANDLE, folder)).is_folder


async def test_a_planted_item_access_is_returned_and_others_allow_everything():
    fake = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    root = (await fake.get_root(HANDLE)).item_id
    plan = fake.plant(HANDLE, root, "Tom's plan.docx", b"plan")
    other = fake.plant(HANDLE, root, "birth-certificate.pdf", b"cert")
    fake.set_access(HANDLE, plan, ItemAccess(can_download=False, can_delete=False))
    assert (await fake.get_item(HANDLE, plan)).access == ItemAccess(can_download=False, can_delete=False)
    assert [c.access for c in await fake.list_children(HANDLE, root) if c.item_id == plan] == [
        ItemAccess(can_download=False, can_delete=False)]
    assert (await fake.get_item(HANDLE, other)).access == ItemAccess()


async def test_capabilities_of_the_default_mode_are_unchanged():
    caps = await FakeDriveAdapter("fake_a").capabilities(HANDLE)
    assert (caps.writable, caps.feed_scope) == (True, FeedScope.AREA)
