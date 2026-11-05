"""`send_file_to_user` on FileManagementAgent (EXTERNAL_FILES_RFC §4.2 D8, D13; U4b B2): the real FileConversionService
under the real agent, a recording stand-in for the notification boundary. Every ref kind is delivered with its bytes,
name, a title naming the store and the sniffed kind and the origin overrides; D13 refuses active email attachments
(never other stores); a refusal is a successful answer, a failed upload a failure."""
import dataclasses
from typing import List, Optional
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.agents.file_management_agent import FileManagementAgent
from src.domain import active_kind as ak
from src.domain.agent import AgentConfig, AgentIntent, AgentMessage, AgentStatus
from src.domain.container_facts import ContainerFacts, ContainerStatus
from src.domain.document_limits import action_cap
from src.domain.document_open_texts import action_too_large_note, folder_note
from src.domain.drive_extraction import ParserUnavailable
from src.domain.drive_feed_scope import FeedScope
from src.domain.email import EmailPart
from src.domain.email_open_texts import ATTACHMENT_MISSING_NOTE, active_kind_refusal_note
from src.domain.email_ref import format_email_ref
from src.infrastructure.agent_manifest import Intent
from src.services.drive_connection_service import DriveConnectionService
from src.services.file_conversion_service import FileConversionService
from tests.support import zip_containers as zc
from tests.support.drive_access_stub import allow_access
from tests.support.drive_connection_repo import FakeDriveConnectionRepository
from tests.support.drive_connections import seed
from tests.support.fake_drive_adapter import FakeDriveAdapter
from tests.support.fake_email_mailbox import FakeEmailMailbox
from tests.unit.services.test_file_conversion_open import CountingStorage, RecordingDocuments
from tests.unit.services.test_file_conversion_open_email import FakeEmailSearch
from tests.unit.services.test_file_conversion_resolve_file import ExportingDrive, FakeContainers
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

PDF = "application/pdf"
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MSG = "msg_invoice042"
CTX = {"user_id": "ann", "account_id": "ann-acc", "origin_channel_id": "C-origin", "origin_platform": "slack"}


class RecordingNotification:
    """The upload boundary: records each call's arguments, returns the scripted bool."""

    def __init__(self, result: bool = True) -> None:
        self.result = result
        self.calls: List[dict] = []

    async def notify_file_bytes(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return self.result


def _part(part_id, name, data_handle, mime=PDF, size=64) -> EmailPart:
    return EmailPart(part_id=part_id, filename=name, mime=mime, size=size, handle=data_handle)


EMAIL_FILES = {
    "1.2": ("invoice_bathroom_es.pdf", PDF, b"%PDF-1.7 invoice"),
    "2": ("notes.txt", "text/plain", b"plain notes"),
    "3": ("garage_rules.html", "text/html", b"<!doctype html><html>rules</html>"),
    "4": ("notes_html.txt", "text/plain", b"<html><body>x</body></html>"),
    "5": ("logo.svg", "image/svg+xml", b"<svg xmlns='http://www.w3.org/2000/svg'></svg>"),
    "6": ("invoice_bathroom_es.docm", "application/vnd.ms-word.document.macroEnabled.12", zc.macro_docm()),
    "7": ("link.lnk", "application/octet-stream",
          b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46" + b"x" * 8),
    "8": ("backup.img", "application/octet-stream", b"plain bytes"),
    "9": ("start.bat", "text/plain", b"echo plain"),
    "10": ("tool.exe", "application/octet-stream", b"MZ\x90\x00\x03\x00\x00\x00\x00\x00"),
    "11": ("setup.sh", "text/plain", b"#!/bin/sh\necho hi\n"),
    "12": ("hta_form.hta", "text/plain", b"<HTA:APPLICATION ID='x'></HTA:APPLICATION>"),
    "13": ("invoice_bathroom_es.docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document", zc.plain_docx()),
    "14": ("photo_roof.png", "image/png", PNG_BYTES),
    "15": ("renamed.pdf", PDF, PNG_BYTES),
    "16": ("huge_scan.pdf", PDF, b"%PDF"),
}


@pytest.fixture
async def world():
    class W:
        pass
    w = W()
    repo = FakeDriveConnectionRepository()
    w.conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-2", tag="b2", kind="fake_b")
    w.drive = ExportingDrive("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.storage = CountingStorage({"invoice_FA-0137.pdf": b"%PDF-chat", "photo_roof.png": PNG_BYTES,
                                 "invoice_bathroom_es.pdf": b"%PDF-bi",
                                 "big.bin": b"x" * (action_cap() + 1)})
    w.media = AsyncMock()
    w.media.fetch.return_value = b"%PDF-delivered"
    w.workspace = InMemoryWorkspaceStore()
    parts = [_part(pid, n, f"h{pid}", mime=m, size=len(d)) for pid, (n, m, d) in EMAIL_FILES.items()
             if pid != "16"]
    parts.append(_part("16", "huge_scan.pdf", "h16", size=action_cap() + 1))
    w.mailbox = FakeEmailMailbox(messages={MSG: parts},
                                 blobs={f"h{pid}": d for pid, (_, _, d) in EMAIL_FILES.items()})
    w.email = FakeEmailSearch(w.mailbox)
    w.containers = FakeContainers()
    w.service = FileConversionService(
        storage=w.storage, media_storage=w.media, drive=w.drive, workspace=w.workspace,
        connections=DriveConnectionService(repo, None), access=allow_access(), email=w.email)
    w.service.attach_documents(RecordingDocuments(), containers=w.containers)
    w.notify = RecordingNotification()
    w.agent = _agent(w.service, w.notify)
    return w


def _agent(service, notify) -> FileManagementAgent:
    config = AgentConfig(agent_id="file_management_agent_ann", agent_type="file_management", capabilities={},
                         metadata={"user_id": "ann"})
    return FileManagementAgent(config=config, conversion_service=service, storage=MagicMock(), notification=notify)


def _message(ref: Optional[str], file_name: Optional[str] = None, context=CTX):
    payload = {"intent": Intent.SEND_FILE_TO_USER}
    if ref is not None:
        payload["file_ref"] = ref
    if file_name is not None:
        payload["file_name"] = file_name
    msg = MagicMock(spec=AgentMessage)
    msg.task_id = "t1"
    msg.intent = AgentIntent.QUERY
    msg.payload = payload
    msg.context = dict(context)
    return msg


async def _send(w, ref, file_name=None):
    return await w.agent.execute(_message(ref, file_name))


def _email(pid):
    return format_email_ref(MSG, pid)


def _plant(w, name, data, mime="", folder=False):
    return f"drive:b2:{w.drive.plant(w.conn.handle, 'root', name, data, mime=mime, folder=folder)}"


# -- delivery ---------------------------------------------------------------------------------------------------------

async def test_a_chat_file_is_delivered_with_the_origin_channel(world):
    response = await _send(world, "invoice_FA-0137.pdf")
    assert response.status == AgentStatus.SUCCESS
    (call,) = world.notify.calls
    assert call == dict(user_id="ann", account_id="ann-acc", file_bytes=b"%PDF-chat", filename="invoice_FA-0137.pdf",
                        title="Chat file (pdf)", channel_id_override="C-origin", platform_override="slack")
    assert response.result == "Sent 'invoice_FA-0137.pdf' (pdf) to the user's chat as a file."


async def test_a_drive_file_is_delivered(world):
    ref = _plant(world, "garage_rules.pdf", b"%PDF-garage", mime=PDF)
    await _send(world, ref)
    (call,) = world.notify.calls
    assert (call["file_bytes"], call["filename"], call["title"]) == (b"%PDF-garage", "garage_rules.pdf",
                                                                      "Drive file (pdf)")


async def test_a_workspace_file_is_delivered(world):
    world.workspace.seed("ann", "projects/invoice_bathroom_es.csv", b"total,1048.45\n")
    await _send(world, "workspace:projects/invoice_bathroom_es.csv")
    (call,) = world.notify.calls
    assert (call["file_bytes"], call["filename"]) == (b"total,1048.45\n", "invoice_bathroom_es.csv")
    assert call["title"].startswith("Workspace file (")


async def test_a_delivered_document_is_delivered(world):
    await _send(world, "docs/ann/report_2026.pdf")
    (call,) = world.notify.calls
    assert (call["file_bytes"], call["filename"], call["title"]) == (b"%PDF-delivered", "report_2026.pdf",
                                                                      "Delivered document (pdf)")


async def test_an_email_attachment_is_delivered_under_its_own_name(world):
    response = await _send(world, _email("1.2"))
    assert response.status == AgentStatus.SUCCESS
    (call,) = world.notify.calls
    assert (call["file_bytes"], call["filename"], call["title"]) == (b"%PDF-1.7 invoice", "invoice_bathroom_es.pdf",
                                                                      "Email attachment (pdf)")
    assert (call["channel_id_override"], call["platform_override"]) == ("C-origin", "slack")


async def test_a_png_named_pdf_is_titled_by_its_sniffed_kind(world):
    await _send(world, _email("15"))
    (call,) = world.notify.calls
    assert call["title"] == "Email attachment (image)" and call["filename"] == "renamed.pdf"


async def test_a_plain_docx_attachment_is_sent_after_the_container_check(world):
    await _send(world, _email("13"))
    assert len(world.notify.calls) == 1 and len(world.containers.calls) == 1


# -- file_name --------------------------------------------------------------------------------------------------------

async def test_a_name_without_extension_keeps_the_source_extension(world):
    await _send(world, "invoice_bathroom_es.pdf", "Bathroom invoice Reformas Exemplo")
    assert world.notify.calls[0]["filename"] == "Bathroom invoice Reformas Exemplo.pdf"


async def test_a_full_name_is_used_and_sanitized(world):
    await _send(world, "invoice_bathroom_es.pdf", "invoice: v2/final.pdf")
    assert world.notify.calls[0]["filename"] == "invoice_ v2_final.pdf"


async def test_an_email_part_renamed_keeps_its_extension(world):
    await _send(world, _email("1.2"), "Bathroom invoice")
    assert world.notify.calls[0]["filename"] == "Bathroom invoice.pdf"


async def test_a_rename_to_an_active_extension_is_refused(world):
    response = await _send(world, _email("2"), "notes.html")
    assert response.status == AgentStatus.SUCCESS
    assert response.result == active_kind_refusal_note(ak.HTML)
    assert world.notify.calls == []


async def test_a_google_native_is_sent_as_markdown(world):
    item_id = world.drive.plant(world.conn.handle, "root", "Q3 plan", b"# plan", mime="text/markdown")
    world.drive.exports.add(item_id)
    await _send(world, f"drive:b2:{item_id}")
    (call,) = world.notify.calls
    assert (call["file_bytes"], call["filename"]) == (b"# plan", "Q3 plan.md")


# -- D13 --------------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("pid, kind", [
    ("3", ak.HTML), ("4", ak.HTML), ("12", ak.HTA), ("5", ak.SVG), ("7", ak.SHORTCUT), ("8", ak.DISK_IMAGE),
    ("9", ak.SCRIPT), ("10", ak.EXECUTABLE), ("11", ak.SCRIPT),
])
async def test_an_active_email_attachment_is_refused_and_not_uploaded(world, pid, kind):
    response = await _send(world, _email(pid))
    assert response.status == AgentStatus.SUCCESS
    assert response.result == active_kind_refusal_note(kind)
    assert world.notify.calls == []


async def test_an_office_file_with_macros_is_refused(world):
    world.containers.facts = ContainerFacts(status=ContainerStatus.ZIP, members=3, vba_project=True)
    response = await _send(world, _email("6"))
    assert response.status == AgentStatus.SUCCESS
    assert response.result == active_kind_refusal_note(ak.OFFICE_MACROS)
    assert world.notify.calls == []


@pytest.mark.parametrize("containers", [FakeContainers(raises=ParserUnavailable("busy")), None])
async def test_a_container_check_that_could_not_run_fails_closed(world, containers):
    world.service.attach_documents(RecordingDocuments(), containers=containers)
    response = await _send(world, _email("13"))
    assert response.status == AgentStatus.SUCCESS
    assert response.result == active_kind_refusal_note(ak.UNCHECKED)
    assert world.notify.calls == []


async def test_an_html_file_from_a_drive_ref_is_sent_without_the_check(world):
    world.service.active_kind_of = AsyncMock(side_effect=AssertionError("D13 is email-only"))
    ref = _plant(world, "garage_rules.html", b"<!doctype html><html>rules</html>", mime="text/html")
    response = await _send(world, ref)
    assert response.status == AgentStatus.SUCCESS
    assert world.notify.calls[0]["filename"] == "garage_rules.html"
    world.service.active_kind_of.assert_not_called()


# -- refusals are answers ---------------------------------------------------------------------------------------------

async def test_a_drive_folder_answers_with_the_folder_hint(world):
    response = await _send(world, _plant(world, "Receipts", b"", folder=True))
    assert response.status == AgentStatus.SUCCESS and response.result == folder_note("Receipts")
    assert world.notify.calls == []


async def test_a_drive_access_refusal_answers_with_its_line(world):
    from src.domain.user_drive import DriveAccessRefused
    ref = _plant(world, "garage_rules.pdf", b"%PDF-g", mime=PDF)
    world.service._access.check_access.side_effect = DriveAccessRefused("[System: outside the scope]")
    response = await _send(world, ref)
    assert response.status == AgentStatus.SUCCESS and response.result == "[System: outside the scope]"
    assert world.notify.calls == []


async def test_a_missing_email_part_answers_with_its_note(world):
    response = await _send(world, _email("99"))
    assert response.status == AgentStatus.SUCCESS and response.result == ATTACHMENT_MISSING_NOTE
    assert world.notify.calls == []


async def test_above_the_cap_answers_with_the_cap_line(world):
    response = await _send(world, "big.bin")
    assert response.status == AgentStatus.SUCCESS
    assert response.result == action_too_large_note("big.bin", action_cap() + 1, action_cap(), "send")
    assert world.notify.calls == []


# -- failures ---------------------------------------------------------------------------------------------------------

async def test_a_failed_upload_is_a_failure(world):
    world.notify.result = False
    response = await _send(world, "invoice_FA-0137.pdf")
    assert response.status == AgentStatus.FAILED and "could not be delivered" in response.error


async def test_no_notification_service_is_a_failure(world):
    agent = _agent(world.service, None)
    response = await agent.execute(_message("invoice_FA-0137.pdf"))
    assert response.status == AgentStatus.FAILED and "not configured" in response.error


async def test_no_file_ref_is_a_failure(world):
    response = await _send(world, None)
    assert response.status == AgentStatus.FAILED and "file_ref" in response.error


async def test_a_missing_chat_file_is_a_failure(world):
    async def missing(filename, user_id):
        raise FileNotFoundError(filename)
    world.storage.download = missing
    response = await _send(world, "gone.pdf")
    assert response.status == AgentStatus.FAILED
    assert world.notify.calls == []
