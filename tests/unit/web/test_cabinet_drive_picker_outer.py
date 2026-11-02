"""The Cabinet folder picker's "Outside this drive's root" section (GOOGLE_DRIVE_RFC §5 G8 item 2, Δ14): the route
answers the folders outside the root beside the root folders, and only without `?parent`. Over the 2b Cabinet harness
with an account-wide fake drive. Invented cast only."""
from dataclasses import replace

from src.domain.drive_connection import DriveConnection
from src.domain.drive_feed_scope import FeedScope
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.web.test_cabinet_drive_connections_2b import OTHER, T0, USER, World, call

BASE = "/api/drive/connections/conn-2/folders"


class OuterWorld(World):
    def __init__(self, feed_scope: FeedScope = FeedScope.ACCOUNT) -> None:
        super().__init__()
        self.adapter = FakeDriveAdapter("fake_b", feed_scope=feed_scope)
        self.drive.__init__(self.adapter, self.connections)
        self.connection = DriveConnection(
            connection_id="conn-2", user_id=USER, account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0,
            write_folder_id=None)
        self.repo.connections[(USER, "conn-2")] = self.connection
        self.root = self.adapter.tree(self.connection.handle).root.item_id
        self.rd = self.adapter.plant(self.connection.handle, self.root, "R&D", folder=True)
        self.coimbra = self.adapter.plant(self.connection.handle, self.root, "Coimbra 2024", folder=True)
        self.hamburg = self.adapter.plant_parentless(self.connection.handle, "Acme Hamburg", folder=True)
        self.q3 = self.adapter.plant(self.connection.handle, self.hamburg, "Q3", folder=True)


async def test_without_a_parent_the_body_carries_the_outer_folders_beside_the_root_folders():
    w = OuterWorld()
    status, body = await call(w.app(), "get", BASE)
    assert status == 200
    assert [f["name"] for f in body["folders"]] == ["Coimbra 2024", "R&D"]
    assert body["outer"] == [{"id": w.hamburg, "name": "Acme Hamburg", "path": "Acme Hamburg"}]


async def test_with_a_parent_the_body_lists_the_children_and_has_no_outer_key():
    w = OuterWorld()
    status, body = await call(w.app(), "get", f"{BASE}?parent={w.hamburg}")
    assert status == 200
    assert [f["name"] for f in body["folders"]] == ["Q3"]
    assert "outer" not in body


async def test_a_scope_put_with_an_inner_outer_folder_is_accepted_and_stored():
    w = OuterWorld()
    status, body = await call(w.app(), "put", "/api/drive/connections/conn-2/scope",
                              {"whole_drive": False, "folder_ids": [w.q3]})
    assert status == 200, body
    assert body["folder_ids"] == [w.q3]
    assert (await w.connections.get(USER, "conn-2")).scope_folder_ids == (w.q3,)


async def test_a_drive_with_one_area_answers_an_empty_outer():
    w = OuterWorld(FeedScope.AREA)
    _, body = await call(w.app(), "get", BASE)
    assert body["outer"] == []
    assert [f["name"] for f in body["folders"]] == ["Coimbra 2024", "R&D"]


async def test_another_users_connection_is_refused_as_before():
    w = OuterWorld()
    foreign = replace(w.connection, connection_id="conn-9", user_id=OTHER)
    w.repo.connections[(OTHER, "conn-9")] = foreign
    status, body = await call(w.app(), "get", "/api/drive/connections/conn-9/folders")
    assert status == 404 and "outer" not in body
