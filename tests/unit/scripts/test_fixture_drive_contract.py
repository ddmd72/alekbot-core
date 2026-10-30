"""T-3: the eval `FixtureDrive` passes the drive contract suite. It is read-only by design beyond `upload` (the mirror's
one write) and is one drive for every handle, so the cases that write further or isolate connections are skipped by
the world's own declaration."""
import pytest

from scripts.validation.drive_eval.fixture_drive import FixtureDrive, item_id_for
from tests.contracts.user_drive_contract import DriveWorld, UserDriveContract
from tests.support.drive_handles import drive_handle

_MANIFEST = {"items": [
    {"path": "plain/bare.bin", "mime": "application/octet-stream", "size": 3},
    {"path": "media/photo.jpg", "mime": "image/jpeg", "size": 10, "facets": {
        "photo": {"takenDateTime": "2025-05-01T10:00:00Z", "cameraMake": "Acme", "cameraModel": "C1"}}},
    {"path": "media/video.mp4", "mime": "video/mp4", "size": 10, "facets": {
        "video": {"duration": 61000, "width": 1920, "height": 1080}}},
    {"path": "media/audio.mp3", "mime": "audio/mpeg", "size": 10, "facets": {"audio": {"duration": 30000}}},
    {"path": "media/location.jpg", "mime": "image/jpeg", "size": 10, "facets": {
        "location": {"latitude": 41.15, "longitude": -8.61}}},
    {"path": "media/package.bin", "size": 10, "facets": {"package": {"type": "oneNote"}}},
    {"path": "media/malware.bin", "size": 10, "facets": {"malware": {}}},
    {"path": "media/alias.bin", "size": 10, "facets": {
        "remoteItem": {"id": "remote-1", "parentReference": {"driveId": "drive-elsewhere"}}}},
    {"path": "plain/closed.bin", "mime": "application/octet-stream", "size": 3, "inaccessible": True},
]}


class _FixtureWorld(DriveWorld):
    id_space = "microsoft_onedrive"
    max_path_chars = 400
    writable = False
    isolates_connections = False
    enforces_path_limit = False

    def __init__(self) -> None:
        self.drive = FixtureDrive(_MANIFEST)
        self.port = self.drive
        self.handle = drive_handle("usr-1", "conn-1", "cred-1")
        self.other = drive_handle("usr-1", "conn-2", "cred-2")

    async def plant_file_without_hash(self, handle, parent_id, name):
        return item_id_for("plain/bare.bin")

    async def plant_media(self, handle, kind):
        names = {"photo": "photo.jpg", "video": "video.mp4", "audio": "audio.mp3", "location": "location.jpg",
                 "package": "package.bin", "malware": "malware.bin", "alias": "alias.bin"}
        return item_id_for(f"media/{names[kind]}")

    async def plant_closed(self, handle, parent_id, name):
        return item_id_for("plain/closed.bin")

    async def plant_thumbnail(self, handle, parent_id, name, data):
        item = await self.drive.upload(handle, parent_id, name, b"img", "image/jpeg")
        self.drive._thumbnails[item.item_id] = data       # the provider's rendition, planted on the provider side
        return item.item_id

    def expired(self, handle, cursor):
        return "not-a-fixture-cursor"

    def in_recycle_bin(self, handle, item_id):
        raise AssertionError("a read-only drive deletes nothing")

    def holds_state_for(self, handle):
        return False


@pytest.fixture
def world():
    return _FixtureWorld()


class TestFixtureDriveContract(UserDriveContract):
    pass
