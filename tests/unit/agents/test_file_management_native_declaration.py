"""Native or text from the consumer's declaration (docs/10_rfcs/EXTERNAL_FILES_RFC.md §4.3, U2 P2-5): an open reads a
file natively only inside a format the message context declares, within its bytes and, for a PDF of a known page count,
its pages. No declaration is text, never native. FileManagement hands its message context to the open unchanged."""
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.drive_text_extent import ExtentState, TextExtent
from src.domain.native_format import NATIVE_FORMATS_KEY, NativeFormat, to_plain
from src.domain.user_drive import DriveItem
from src.infrastructure.agent_manifest import Intent
from src.infrastructure.drive_operations import DriveOperations
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from src.services.user_drive_service import UserDriveService
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_service_connections import seed_user, seeded_connections

MIB = 1024 * 1024
PNG = DriveItem(item_id="i2", name="a.png", path="a.png", is_folder=False, size_bytes=4 * MIB, mime_type="image/png")
PDF = DriveItem(item_id="p2", name="lease.pdf", path="lease.pdf", is_folder=False, size_bytes=2 * MIB,
                mime_type="application/pdf")
HANDLE = seed_user(FakeDriveConnectionRepository(), "u1").handle

IMAGES = NativeFormat("image/png", 3 * MIB + MIB // 4, 1)          # 3.25 MiB
PDFS = NativeFormat("application/pdf", 20 * MIB, 100)


def _ctx(*formats):
    ctx = {"user_id": "u1", "account_id": "a1"}
    if formats:
        ctx[NATIVE_FORMATS_KEY] = to_plain(formats)
    return ctx


def _msg(ctx, **payload):
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = {"intent": Intent.OPEN_FILE, **payload}
    msg.context = ctx
    return msg


@pytest.fixture
def conversion():
    c = AsyncMock(spec=FileConversionService)
    c.resolve_bytes.return_value = b"%PDF-1.4"
    c.resolve_content.return_value = "converted text"
    return c


@pytest.fixture
def drive():
    d = AsyncMock(spec=UserDriveService)
    reads = UserDriveService(drive=None, connections=seeded_connections("u1"))
    d.resolve_ref.side_effect = reads.resolve_ref
    d.primary.side_effect = reads.primary
    d.mounts.side_effect = reads.mounts
    return d


@pytest.fixture
def ops(conversion, drive):
    return DriveOperations(conversion_service=conversion, notification=AsyncMock(), drive_service=drive,
                           localization=MagicMock(), language_service=AsyncMock())


@pytest.fixture
def agent(conversion, drive, ops):
    return FileManagementAgent(
        config=AgentConfig(agent_id="file_management_agent_u1", agent_type="file_management",
                           capabilities={}, metadata={}),
        conversion_service=conversion, storage=AsyncMock(spec=FileStoragePort), notification=AsyncMock(),
        drive_service=drive, drive_operations=ops,
    )


class TestStoredFiles:
    async def test_no_declaration_reads_a_pdf_as_text(self, agent, conversion):
        resp = await agent.execute(_msg(_ctx(), file_ref="lease.pdf"))
        assert resp.status == AgentStatus.SUCCESS and "file_data" not in resp.metadata
        conversion.resolve_bytes.assert_not_called()
        conversion.resolve_content.assert_awaited_once_with("lease.pdf", "u1")

    async def test_declared_pdf_is_native(self, agent, conversion):
        resp = await agent.execute(_msg(_ctx(PDFS), file_ref="lease.pdf"))
        assert resp.metadata["file_data"]["mime_type"] == "application/pdf"
        conversion.resolve_content.assert_not_called()

    async def test_a_pdf_not_declared_is_text_though_images_are(self, agent, conversion):
        resp = await agent.execute(_msg(_ctx(IMAGES), file_ref="lease.pdf"))
        assert "file_data" not in resp.metadata
        conversion.resolve_content.assert_awaited_once()

    async def test_a_file_above_the_declared_bytes_takes_the_converter(self, agent, conversion):
        conversion.resolve_bytes.return_value = b"x" * (IMAGES.max_bytes + 1)
        resp = await agent.execute(_msg(_ctx(IMAGES), file_ref="photo.png"))
        assert "file_data" not in resp.metadata
        conversion.resolve_content.assert_awaited_once_with("photo.png", "u1")

    async def test_a_file_at_the_declared_bytes_is_native(self, agent, conversion):
        conversion.resolve_bytes.return_value = b"x" * IMAGES.max_bytes
        resp = await agent.execute(_msg(_ctx(IMAGES), file_ref="photo.png"))
        assert resp.metadata["file_data"]["mime_type"] == "image/png"

    async def test_a_malformed_declaration_is_a_failure_not_a_guess(self, agent):
        ctx = {"user_id": "u1", NATIVE_FORMATS_KEY: [{"mime": "image/png"}]}
        resp = await agent.execute(_msg(ctx, file_ref="photo.png"))
        assert resp.status == AgentStatus.FAILED


class TestDriveFiles:
    async def test_no_declaration_reads_an_image_as_text(self, ops, conversion, drive):
        drive.get_item.return_value = PNG
        out = await ops.open(_ctx(), "u1", "drive:a1:i2")
        assert out.file_data is None and "converted text" in out.text
        conversion.resolve_bytes.assert_not_called()

    async def test_image_within_the_declared_bytes_is_native(self, ops, conversion, drive):
        drive.get_item.return_value = PNG
        out = await ops.open(_ctx(NativeFormat("image/png", 5 * MIB, 1)), "u1", "drive:a1:i2")
        assert out.file_data["mime_type"] == "image/png"

    async def test_image_between_a_smaller_cap_and_five_mib_is_text(self, ops, conversion, drive):
        drive.get_item.return_value = PNG   # 4 MiB, cap 3.25 MiB
        out = await ops.open(_ctx(IMAGES), "u1", "drive:a1:i2")
        assert out.file_data is None
        conversion.resolve_bytes.assert_not_called()

    async def test_pdf_over_the_declared_pages_is_text(self, ops, conversion, drive):
        drive.get_item.return_value = PDF
        ops._text_extent = AsyncMock(return_value=TextExtent(ExtentState.NOT_INDEXED, pages_total=101))
        out = await ops.open(_ctx(PDFS), "u1", "drive:a1:p2")
        assert out.file_data is None
        conversion.resolve_bytes.assert_not_called()

    async def test_pdf_within_the_declared_pages_is_native(self, ops, conversion, drive):
        drive.get_item.return_value = PDF
        ops._text_extent = AsyncMock(return_value=TextExtent(ExtentState.NOT_INDEXED, pages_total=100))
        out = await ops.open(_ctx(PDFS), "u1", "drive:a1:p2")
        assert out.file_data["mime_type"] == "application/pdf"

    async def test_pdf_of_unknown_pages_is_native_by_bytes(self, ops, drive):
        drive.get_item.return_value = PDF
        out = await ops.open(_ctx(PDFS), "u1", "drive:a1:p2")
        assert out.file_data is not None

    async def test_pdf_above_the_declared_bytes_is_text(self, ops, conversion, drive):
        drive.get_item.return_value = PDF
        out = await ops.open(_ctx(NativeFormat("application/pdf", MIB, 100)), "u1", "drive:a1:p2")
        assert out.file_data is None
        conversion.resolve_bytes.assert_not_called()
