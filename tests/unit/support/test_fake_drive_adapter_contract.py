"""T-3: `FakeDriveAdapter` passes the drive contract suite (one run per path limit it can declare, T-7)."""
from datetime import datetime, timezone

import pytest

from src.domain.drive_media_facts import DriveMediaFacts
from tests.contracts.user_drive_contract import DriveWorld, UserDriveContract
from tests.support.drive_handles import drive_handle
from tests.support.fake_drive_adapter import FakeDriveAdapter

_MEDIA = {
    "photo": DriveMediaFacts(taken_at=datetime(2025, 5, 1, 10, 0, tzinfo=timezone.utc), camera_make="Acme",
                             camera_model="C1"),
    "video": DriveMediaFacts(video_duration_ms=61000, video_width=1920, video_height=1080),
    "audio": DriveMediaFacts(audio_duration_ms=30000),
    "location": DriveMediaFacts(latitude=41.15, longitude=-8.61),
    "package": DriveMediaFacts(package=True),
    "malware": DriveMediaFacts(malware=True),
    "alias": DriveMediaFacts(alias_of=""),
}


class _FakeWorld(DriveWorld):
    def __init__(self, max_path_chars: int) -> None:
        self.id_space = "fake_a"
        self.max_path_chars = max_path_chars
        self.fake = FakeDriveAdapter("fake_a", max_path_chars=max_path_chars)
        self.port = self.fake
        self.handle = drive_handle("usr-1", "conn-1", "cred-1", "fake_a")
        self.other = drive_handle("usr-1", "conn-2", "cred-2", "fake_a")

    async def _root(self, handle):
        return (await self.fake.get_root(handle)).item_id

    async def plant_file_without_hash(self, handle, parent_id, name):
        return self.fake.plant(handle, parent_id, name, b"bare", hashed=False)

    async def plant_media(self, handle, kind):
        return self.fake.plant(handle, await self._root(handle), f"{kind}.bin", b"m", media=_MEDIA[kind])

    async def plant_closed(self, handle, parent_id, name):
        item_id = self.fake.plant(handle, parent_id, name, b"closed")
        self.fake.close(handle, item_id)
        return item_id

    async def plant_thumbnail(self, handle, parent_id, name, data):
        return self.fake.plant(handle, parent_id, name, b"img", thumbnail=data)

    def expired(self, handle, cursor):
        self.fake.expire_cursors(handle)
        return cursor

    def in_recycle_bin(self, handle, item_id):
        return self.fake.in_recycle_bin(handle, item_id)

    def holds_state_for(self, handle):
        return self.fake.holds_state_for(handle)


@pytest.fixture(params=[255, 400])
def world(request):
    return _FakeWorld(request.param)


class TestFakeDriveAdapterContract(UserDriveContract):
    pass
