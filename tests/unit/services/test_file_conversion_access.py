"""Drive refs through `FileConversionService` obey the access policy (review R1 B1, N1; MULTI_DRIVE_RFC §11.3): the READ
check runs after the metadata read and before any download, in all three drive methods. These are the entry points
workspace copy, image/video generation and skills reach with a model-supplied ref. `access` is a required argument."""
import inspect
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE
from src.domain.drive_connection import DriveConnection
from src.domain.user_drive import DriveAccessRefused
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter

T0 = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)


class World:
    def __init__(self):
        self.fake = FakeDriveAdapter("fake_b")
        self.repo = FakeDriveConnectionRepository()
        self.connection = DriveConnection(
            connection_id="conn-2", user_id="usr-1", account_id="acc-1", credential_id="cred-2", kind="fake_b",
            drive_identity="fake_b:d2", ref_tag="b2", mount_name="Acme", created_at=T0)
        self.repo.connections[("usr-1", "conn-2")] = self.connection
        self.handle = self.connection.handle
        connections = DriveConnectionService(self.repo, None)
        self.drive_service = UserDriveService(self.fake, connections)
        self.conversion = FileConversionService(storage=AsyncMock(spec=FileStoragePort), drive=self.fake,
                                                connections=connections, access=self.drive_service)
        root = self.fake.tree(self.handle).root.item_id
        self.rd = self.fake.plant(self.handle, root, "R&D", folder=True)
        self.plan = self.fake.plant(self.handle, self.rd, "Tom's plan.txt", b"plan")
        self.coimbra = self.fake.plant(self.handle, root, "Coimbra 2024", folder=True)
        self.lab = self.fake.plant(self.handle, self.coimbra, "lab-report.txt", b"lab")
        self.repo.connections[("usr-1", "conn-2")] = replace(self.connection, scope_folder_ids=(self.rd,))
        self.fake.calls.clear()

    def ref(self, item_id):
        return f"drive:b2:{item_id}"

    def downloads(self):
        return [m for m, _ in self.fake.calls if m == "download"]


def test_access_is_a_required_argument():
    parameter = inspect.signature(FileConversionService.__init__).parameters["access"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("method", ["resolve_bytes", "resolve_content", "get_drive_item"])
async def test_an_out_of_scope_ref_is_refused_before_any_download(method):
    w = World()
    with pytest.raises(DriveAccessRefused) as caught:
        await getattr(w.conversion, method)(w.ref(w.lab), "usr-1")
    assert caught.value.line == OUTSIDE_SCOPE_LINE.format(drive="Acme")
    assert w.downloads() == []


async def test_an_in_scope_ref_is_read():
    w = World()
    assert await w.conversion.resolve_bytes(w.ref(w.plan), "usr-1") == b"plan"
    assert "plan" in await w.conversion.resolve_content(w.ref(w.plan), "usr-1")
    assert (await w.conversion.get_drive_item(w.ref(w.plan), "usr-1")).item_id == w.plan


async def test_a_folder_out_of_scope_does_not_print_its_path():
    w = World()
    with pytest.raises(DriveAccessRefused) as caught:
        await w.conversion.resolve_content(w.ref(w.coimbra), "usr-1")
    assert "Coimbra" not in caught.value.line
