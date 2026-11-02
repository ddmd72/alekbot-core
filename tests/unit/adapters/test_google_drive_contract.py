"""T-3: the Google drive adapter passes the drive contract suite over a stateful Drive v3 at the aiohttp boundary
(GOOGLE_DRIVE_RFC §6 row "Contract").

Two accounts, two bearer tokens (`tests/support/google_drive_fake.py`): a call that carries the wrong grant lands in the
wrong account. The credentials are the real store's (`tests/support/oauth_credentials_store.py`).

What the world cannot plant, and why: Drive v3 has no audio, package or malware facet (RFC §3.4: `package`, `malware`
False; no audio metadata exists), so those three media kinds are skipped; Drive documents no path length (only 100
folder levels, mapped to DrivePathTooLong by reason in the errors wire test), so `enforces_path_limit` is False.
"""
from dataclasses import replace

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.domain.drive_feed_scope import FeedScope
from tests.contracts.user_drive_contract import DriveWorld, UserDriveContract
from tests.support.google_drive_fake import google_setup

_IMAGE = {"time": "2025:05:01 10:00:00", "cameraMake": "Acme", "cameraModel": "C1", "width": 4000, "height": 3000}


class _GoogleWorld(DriveWorld):
    id_space = "google_drive"
    max_path_chars = 32_768
    feed_scope = FeedScope.ACCOUNT
    enforces_path_limit = False
    item_identity_in_drive = False

    def __init__(self, setup):
        self.setup, self.adapter, self.port, self.fake = setup, setup.adapter, setup.adapter, setup.fake
        self.handle, self.other = setup.handle, setup.other
        self._accounts = {h.connection_id: a for h, a in zip(setup.handles, setup.accounts)}

    def account(self, handle):
        return self._accounts[handle.connection_id]

    async def plant_file_without_hash(self, handle, parent_id, name):
        return self.account(handle).add_file(parent_id, name, b"bare", hashed=False).id

    async def plant_media(self, handle, kind):
        account = self.account(handle)
        root = account.root.id
        if kind == "photo":
            return account.add_file(root, "photo.jpg", b"m", mime="image/jpeg", image=dict(_IMAGE)).id
        if kind == "video":
            return account.add_file(root, "Hamburg talk.mp4", b"m", mime="video/mp4",
                                    video={"durationMillis": "61000", "width": 1920, "height": 1080}).id
        if kind == "location":
            return account.add_file(root, "garden.jpg", b"m", mime="image/jpeg",
                                    image={"location": {"latitude": 41.15, "longitude": -8.61, "altitude": 12.0}}).id
        if kind == "alias":
            return account.add_shortcut(root, "Q3 budget", target="fid-elsewhere").id
        if kind in ("audio", "package", "malware"):
            pytest.skip(f"Google Drive has no {kind} facet (GOOGLE_DRIVE_RFC §3.4)")
        raise AssertionError(f"the Google world plants no {kind!r} item")

    async def plant_closed(self, handle, parent_id, name):
        account = self.account(handle)
        node = account.add_file(parent_id, name, b"closed")
        account.closed.add(node.id)
        return node.id

    async def plant_thumbnail(self, handle, parent_id, name, data):
        return self.account(handle).add_file(parent_id, name, b"img", mime="image/jpeg", thumbnail=data).id

    def expired(self, handle, cursor):
        self.account(handle).expired = True
        return cursor

    def in_recycle_bin(self, handle, item_id):
        node = self.account(handle).files.get(item_id)
        return node is not None and node.trashed

    def holds_state_for(self, handle):
        adapter = self.adapter
        return ((handle.user_id, handle.credential_id) in adapter._roots or handle.connection_id in adapter._paths
                or handle.connection_id in adapter._outer
                or adapter._tokens.holds(handle.user_id, handle.credential_id))

    async def plant_outer_folder(self, handle, name):
        return self.account(handle).add_folder(None, name).id

    async def share_with(self, handle, item_id, other):
        # shared without its folder: the other account sees the same file id and no parent of it
        node = self.account(handle).files[item_id]
        self.account(other)._add(replace(node, parents=[]))


@pytest.fixture
async def world(monkeypatch):
    return _GoogleWorld(await google_setup(monkeypatch))


class TestGoogleDriveContract(UserDriveContract):
    async def test_the_world_is_the_google_drive_spec(self, world):
        spec = ConnectorRegistry().get("google_drive")
        assert world.id_space == spec.id_space
        assert (await world.port.capabilities(world.handle)).max_path_chars == world.max_path_chars

    async def test_every_request_carries_its_own_connections_grant(self, world):
        a, b = world.handle, world.other
        root_a = await world.port.get_root(a)
        root_b = await world.port.get_root(b)
        await world.port.list_children(b, root_b.item_id)
        await world.port.list_children(a, root_a.item_id)
        bearers = [r.bearer for r in world.fake.requests]
        assert set(bearers) == {"tok-a", "tok-b"}
        assert root_a.drive_identity == "google_drive:account:perm-a"
        assert root_b.drive_identity == "google_drive:account:perm-b"
        assert world.fake.unrouted == []
