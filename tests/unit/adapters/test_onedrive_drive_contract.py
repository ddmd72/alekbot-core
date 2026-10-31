"""T-3: the OneDrive adapter passes the drive contract suite over a stateful Graph at the aiohttp boundary.

Two accounts, two bearer tokens, two drives (`tests/support/graph_drive_fake.py`): a call that carries the wrong grant
lands in the wrong drive. The credentials are the real store's (`tests/support/oauth_credentials_store.py`)."""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.microsoft.onedrive_adapter import OneDriveAdapter
from src.composition.connector_registry import ConnectorRegistry
from tests.contracts.user_drive_contract import DriveWorld, UserDriveContract
from tests.support.drive_handles import drive_handle
from tests.support.graph_drive_fake import MAX_PATH, GraphDriveFake
from tests.support.oauth_credentials_store import CredentialsStore, creds

_EXTRA = {
    "photo": {"photo": {"takenDateTime": "2025-05-01T10:00:00Z", "cameraMake": "Acme", "cameraModel": "C1"},
              "image": {"width": 4000, "height": 3000}},
    "video": {"video": {"duration": 61000, "width": 1920, "height": 1080}},
    "audio": {"audio": {"duration": 30000}},
    "location": {"location": {"latitude": 41.15, "longitude": -8.61}},
    "package": {"package": {"type": "oneNote"}},
    "malware": {"malware": {}},
    "alias": {"remoteItem": {"id": "remote-1", "parentReference": {"driveId": "drive-elsewhere"}}},
}


class _OneDriveWorld(DriveWorld):
    id_space = "microsoft_onedrive"
    max_path_chars = 400

    def __init__(self, fake, adapter, handles, drives):
        self.fake, self.adapter, self.port = fake, adapter, adapter
        self.handle, self.other = handles
        self._drives = {h.connection_id: d for h, d in zip(handles, drives)}

    def _drive(self, handle):
        return self._drives[handle.connection_id]

    async def plant_file_without_hash(self, handle, parent_id, name):
        return self._drive(handle).add(parent_id, name, data=b"bare", hashed=False).item_id

    async def plant_media(self, handle, kind):
        drive = self._drive(handle)
        return drive.add(drive.root.item_id, f"{kind}.bin", data=b"m", extra=_EXTRA[kind]).item_id

    async def plant_closed(self, handle, parent_id, name):
        drive = self._drive(handle)
        node = drive.add(parent_id, name, data=b"closed")
        drive.closed.add(node.item_id)
        return node.item_id

    async def plant_thumbnail(self, handle, parent_id, name, data):
        return self._drive(handle).add(parent_id, name, data=b"img", mime="image/jpeg", thumbnail=data).item_id

    def expired(self, handle, cursor):
        self._drive(handle).expired = True
        return cursor

    def in_recycle_bin(self, handle, item_id):
        return item_id in self._drive(handle).recycle

    def holds_state_for(self, handle):
        return (self.adapter._tokens.cached_token(handle.user_id, handle.credential_id) is not None
                or handle.connection_id in self.adapter._root_cache)


@pytest.fixture
async def world(monkeypatch):
    fake = GraphDriveFake().install(monkeypatch)
    store = CredentialsStore()
    expiry = datetime.now(timezone.utc) + timedelta(hours=1)
    handles, drives = [], []
    for n, (token, drive_id) in enumerate((("tok-a", "drive-a"), ("tok-b", "drive-b")), start=1):
        drives.append(fake.add_drive(token, drive_id))
        credential_id = await store.repo.save(creds("usr-1", "microsoft_onedrive", access_token=token,
                                                    token_expiry=expiry,
                                                    account=_account(n)))
        handles.append(drive_handle("usr-1", f"conn-{n}", credential_id))
    adapter = OneDriveAdapter(store.repo, "cid", "csecret", connector=ConnectorRegistry().get("microsoft_onedrive"))
    return _OneDriveWorld(fake, adapter, handles, drives)


def _account(n):
    from src.domain.provider_account import ProviderAccount
    return ProviderAccount(realm="microsoft", subject=f"subject-{n}", label=f"owner{n}@example.com")


class TestOneDriveContract(UserDriveContract):
    async def test_the_path_limit_is_the_providers(self, world):
        assert world.max_path_chars == MAX_PATH

    async def test_every_request_carries_its_own_connections_grant(self, world):
        a, b = world.handle, world.other
        await world.port.get_root(a)
        await world.port.get_root(b)
        await world.port.list_children(b, (await world.port.get_root(b)).item_id)
        await world.port.list_children(a, (await world.port.get_root(a)).item_id)
        bearers = [r.bearer for r in world.fake.requests if r.url.startswith("https://graph")]
        assert set(bearers) == {"tok-a", "tok-b"}
        assert bearers[0] == "tok-a" and bearers[1] == "tok-b"
        assert world.fake.unrouted == []
