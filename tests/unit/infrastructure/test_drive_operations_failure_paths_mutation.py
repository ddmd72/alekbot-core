"""DriveOperations failure paths (mutation survivors): a failed connector folder read leaves the paths out of the
connector's `/` listing, and a delivered video that cannot be transferred is a refusal with its reason."""
import dataclasses
import logging

from src.domain.drive_audience import connector_audience
from src.domain.drive_connector_access import ConnectorAccess
from src.domain.drive_op import DriveOp
from src.domain.drive_outcome_class import OutcomeClass
from src.domain.exceptions import EmailFileNotTransferable
from src.domain.user_drive import DriveItem
from src.infrastructure.drive_operations import DriveOperations
from tests.support.drive_service_connections import CONNECTION_ID, ConnectionReads

CTX = {"user_id": "u1", "account_id": "a1", "origin_channel_id": "C1", "origin_platform": "slack"}
CLIP = DriveItem(item_id="v1", name="clip.mp4", path="Videos/clip.mp4", is_folder=False, size_bytes=5,
                 mime_type="video/mp4")


class Drive(ConnectionReads):
    def __init__(self, paths=None):
        self.paths = paths

    def _reads(self, user_id):
        service = super()._reads(user_id)
        repo = self._reads_repo
        key = (user_id, CONNECTION_ID)
        if repo.connections[key].connector_access is None:
            repo.connections[key] = dataclasses.replace(repo.connections[key],
                                                        connector_access=ConnectorAccess.whole_drive())
        return service

    async def connector_folder_paths(self, handle):
        if self.paths is None:
            raise RuntimeError("provider down")
        return self.paths, 0

    async def get_item(self, handle, item_id):
        return CLIP


class Conversion:
    async def get_drive_item(self, ref, user_id):
        return CLIP

    async def resolve_bytes(self, ref, user_id):
        raise EmailFileNotTransferable("This file cannot be transferred.")


async def _listing(drive):
    ops = DriveOperations(conversion_service=Conversion(), drive_service=drive)
    with connector_audience():
        return await ops.list_folder(CTX, "u1", "/")


async def test_a_failed_connector_folder_read_leaves_the_paths_out(caplog):
    with caplog.at_level(logging.WARNING):
        out = await _listing(Drive(paths=None))
    assert out.cls is OutcomeClass.OK and out.success
    assert "connector folders read failed" in caplog.text
    assert out.text.startswith("Drives (") and "- Drive:" in out.text


async def test_a_video_that_cannot_be_transferred_is_a_refusal_with_its_reason():
    ops = DriveOperations(conversion_service=Conversion(), drive_service=Drive())
    out = await ops.fetch_stored(CTX, "u1", "workspace:binary_files/clip.mp4")
    assert (out.op, out.cls, out.success) == (DriveOp.OPEN, OutcomeClass.REFUSED, False)
    assert out.text == "This file cannot be transferred."
