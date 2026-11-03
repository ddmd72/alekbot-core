"""Drive refs through `FileConversionService` obey the access policy (review R1 B1, N1; MULTI_DRIVE_RFC §11.3): the READ
check runs after the metadata read and before any download, in all three drive methods (`open` replaced
`resolve_content`, EXTERNAL_FILES_RFC §4.2). These are the entry points workspace copy, image/video generation and
skills reach with a model-supplied ref. `access` is a required argument."""
import inspect
import time
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src.domain.drive_access_policy import OUTSIDE_SCOPE_LINE
from src.domain.drive_connection import DriveConnection
from src.domain.user_drive import DriveAccessRefused
from src.ports.file_storage_port import FileStoragePort
from src.services.drive_connection_service import DriveConnectionService
from src.services.document_service import DocumentService
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.unit.services.test_document_service_read import FakeExtractor, FakeSplitter

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
        self.conversion.attach_documents(DocumentService(FakeExtractor("parser"), FakeExtractor("reader"),
                                                         FakeSplitter()))
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
        # both download methods: `open` streams a text file with download_to_file
        return [m for m, _ in self.fake.calls if m in ("download", "download_to_file")]

    async def open(self, ref):
        return await self.conversion.open(ref, "usr-1", "acc-1", deadline_at=time.time() + 60)


_CALLS = {
    "resolve_bytes": lambda w, ref: w.conversion.resolve_bytes(ref, "usr-1"),
    "open": lambda w, ref: w.open(ref),
    "get_drive_item": lambda w, ref: w.conversion.get_drive_item(ref, "usr-1"),
}


def test_access_is_a_required_argument():
    parameter = inspect.signature(FileConversionService.__init__).parameters["access"]
    assert parameter.default is inspect.Parameter.empty


@pytest.mark.parametrize("method", list(_CALLS))
async def test_an_out_of_scope_ref_is_refused_before_any_download(method):
    w = World()
    with pytest.raises(DriveAccessRefused) as caught:
        await _CALLS[method](w, w.ref(w.lab))
    assert caught.value.line == OUTSIDE_SCOPE_LINE.format(drive="Acme")
    assert w.downloads() == []


async def test_an_in_scope_ref_is_read():
    w = World()
    assert await w.conversion.resolve_bytes(w.ref(w.plan), "usr-1") == b"plan"
    assert "plan" in (await w.open(w.ref(w.plan))).body
    assert (await w.conversion.get_drive_item(w.ref(w.plan), "usr-1")).item_id == w.plan


async def test_a_folder_out_of_scope_does_not_print_its_path():
    w = World()
    with pytest.raises(DriveAccessRefused) as caught:
        await w.open(w.ref(w.coimbra))
    assert "Coimbra" not in caught.value.line
