"""`FileConversionService.resolve_file` and `.active_kind_of` (EXTERNAL_FILES_RFC §4.2 "Resolver callers", §4.5 "Bytes for
an action", D13; U4b core): every ref kind's bytes, name and mime under the action cap, the email branch with one
listing and one download and every note, `resolve_bytes` still refusing `email:`, and D13's check failing closed when
the sandbox's container facts are missing. The real service over the drive, workspace, storage and mailbox fakes."""
import dataclasses
import logging
from typing import List, Optional
from unittest.mock import AsyncMock

import pytest

from src.domain import active_kind as ak
from src.domain.container_facts import ContainerFacts, ContainerStatus
from src.domain.document_limits import action_cap
from src.domain.document_open import ResolvedFile
from src.domain.document_open_texts import action_too_large_note, folder_note
from src.domain.drive_extraction import ParserUnavailable
from src.domain.drive_feed_scope import FeedScope
from src.domain.email import EmailPart
from src.domain.email_open_texts import (
    ATTACHMENT_EMPTY_NOTE, ATTACHMENT_MISSING_NOTE, EMAIL_GONE_NOTE, EMAIL_NOT_CONNECTED_NOTE,
    EMAIL_OPEN_NOT_CONFIGURED_NOTE, MAILBOX_UNAVAILABLE_NOTE,
)
from src.domain.email_ref import format_email_ref
from src.domain.exceptions import EmailFileNotTransferable, EmailPartUnavailable, FileNotResolved
from src.ports.container_facts_port import ContainerFactsPort
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
from tests.unit.services.workspace_fakes import InMemoryWorkspaceStore

MB = 1024 * 1024
PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
DEADLINE = 1_900_000_000.0
MSG = "msg_quote042"


class FakeContainers(ContainerFactsPort):
    """The sandbox's container reader: answers the scripted facts, or raises; records each call's bytes."""

    def __init__(self, facts: Optional[ContainerFacts] = None, raises: Optional[Exception] = None) -> None:
        self.facts = facts or ContainerFacts(status=ContainerStatus.ZIP, members=2)
        self.raises = raises
        self.calls: List[bytes] = []

    async def container_facts(self, data, *, deadline_at=None):
        self.calls.append(data)
        if self.raises:
            raise self.raises
        return self.facts


def _part(part_id, name, size, handle, mime=PDF) -> EmailPart:
    return EmailPart(part_id=part_id, filename=name, mime=mime, size=size, handle=handle)


@pytest.fixture
async def world():
    class World:
        pass
    w = World()
    repo = FakeDriveConnectionRepository()
    w.conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-2", tag="b2", kind="fake_b")
    w.drive = FakeDriveAdapter("fake_b", feed_scope=FeedScope.ACCOUNT)
    w.storage = CountingStorage({"invoice_FA-0137.pdf": b"%PDF-chat", "big.bin": b"x" * (action_cap() + 1)})
    w.media = AsyncMock()
    w.media.fetch.return_value = b"%PDF-delivered"
    w.workspace = InMemoryWorkspaceStore()
    w.mailbox = FakeEmailMailbox(
        messages={MSG: [_part("1.2", "bathroom_quote.pdf", 2048, "h-quote"),
                        _part("1.3", "bathroom_quote.docx", 4096, "h-docx", mime=DOCX),
                        _part("2", "empty.txt", 0, "", mime="text/plain"),
                        _part("3", "huge_scan.pdf", action_cap() + 1, "h-huge")]},
        blobs={"h-quote": b"%PDF-quote", "h-docx": zc.macro_docm(), "h-huge": b"%PDF"})
    w.email = FakeEmailSearch(w.mailbox)
    w.containers = FakeContainers()
    w.service = FileConversionService(
        storage=w.storage, media_storage=w.media, drive=w.drive, workspace=w.workspace,
        connections=DriveConnectionService(repo, None), access=allow_access(), email=w.email)
    w.service.attach_documents(RecordingDocuments(), containers=w.containers)
    return w


def _plant(w, name, data, mime="", folder=False):
    return f"drive:b2:{w.drive.plant(w.conn.handle, 'root', name, data, mime=mime, folder=folder)}"


# -- per ref kind ----------------------------------------------------------------------------------------------------

async def test_an_email_ref_resolves_with_one_listing_and_one_download(world):
    resolved = await world.service.resolve_file(format_email_ref(MSG, "1.2"), "ann", action="send")
    assert resolved == ResolvedFile(data=b"%PDF-quote", name="bathroom_quote.pdf", mime=PDF)
    assert world.email.listings == 1 and [d[:2] for d in world.email.downloads] == [(MSG, "1.2")]
    assert world.email.downloads[0][2] == action_cap()


async def test_a_drive_ref_resolves_with_the_item_s_name_and_mime(world):
    ref = _plant(world, "garage_rules.pdf", b"%PDF-garage", mime=PDF)
    resolved = await world.service.resolve_file(ref, "ann", action="save")
    assert resolved == ResolvedFile(data=b"%PDF-garage", name="garage_rules.pdf", mime=PDF)
    world.service._access.check_access.assert_awaited()   # the READ check ran before the download


class ExportingDrive(FakeDriveAdapter):
    """A drive whose items named in `exports` are provider exports (a Google native: `DriveItem.exported`)."""

    def __init__(self, *a, **k) -> None:
        super().__init__(*a, **k)
        self.exports: set = set()

    async def get_item(self, handle, item_id):
        item = await super().get_item(handle, item_id)
        return dataclasses.replace(item, exported=item_id in self.exports)


@pytest.mark.parametrize("name, export_mime, served", [
    ("Q3 plan", "text/markdown", "Q3 plan.md"),
    ("Q3 plan.md", "text/markdown", "Q3 plan.md"),                     # never doubled
    ("Budget 2026", XLSX, "Budget 2026.xlsx"),
    ("Hamburg talk", "text/plain", "Hamburg talk.txt"),
    ("Garden plan 2026", PDF, "Garden plan 2026.pdf"),
    ("brief.docx", "text/markdown", "brief.docx.md"),                  # the bytes are Markdown, not a docx
])
async def test_a_provider_export_carries_the_export_mime_and_its_extension(name, export_mime, served):
    repo = FakeDriveConnectionRepository()
    conn = await seed(repo, user_id="ann", account_id="ann", connection_id="conn-g", tag="g1", kind="fake_g")
    drive = ExportingDrive("fake_g", feed_scope=FeedScope.ACCOUNT)
    item_id = drive.plant(conn.handle, "root", name, b"exported bytes", mime=export_mime)
    drive.exports.add(item_id)
    service = FileConversionService(storage=CountingStorage(), drive=drive,
                                    connections=DriveConnectionService(repo, None), access=allow_access())
    resolved = await service.resolve_file(f"drive:g1:{item_id}", "ann", action="send")
    assert resolved == ResolvedFile(data=b"exported bytes", name=served, mime=export_mime)


async def test_an_ordinary_drive_file_keeps_its_name_whatever_its_mime(world):
    ref = _plant(world, "Q3 plan", b"# plan", mime="text/markdown")   # not an export: no extension is invented
    assert (await world.service.resolve_file(ref, "ann", action="send")).name == "Q3 plan"


async def test_a_chat_ref_resolves_from_the_store(world):
    resolved = await world.service.resolve_file("invoice_FA-0137.pdf", "ann", action="send")
    assert resolved == ResolvedFile(data=b"%PDF-chat", name="invoice_FA-0137.pdf", mime=PDF)


async def test_a_workspace_ref_resolves_by_its_path(world):
    world.workspace.seed("ann", "projects/budget_2026.csv", b"rent,11400\n")
    resolved = await world.service.resolve_file("workspace:projects/budget_2026.csv", "ann", action="send")
    assert (resolved.data, resolved.name, resolved.mime) == (b"rent,11400\n", "budget_2026.csv", "text/csv")


async def test_a_delivered_document_resolves_after_the_ownership_check(world):
    resolved = await world.service.resolve_file("docs/ann/report_2026.pdf", "ann", action="send")
    assert resolved == ResolvedFile(data=b"%PDF-delivered", name="report_2026.pdf", mime=PDF)
    with pytest.raises(PermissionError):
        await world.service.resolve_file("docs/someone_else/report_2026.pdf", "ann", action="send")


# -- the action cap ------------------------------------------------------------------------------------------------

async def test_an_email_part_above_the_cap_is_refused_on_the_listing(world):
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref(MSG, "3"), "ann", action="send")
    assert raised.value.line == action_too_large_note("huge_scan.pdf", action_cap() + 1, action_cap(), "send")
    assert world.email.downloads == []


async def test_a_drive_item_above_the_cap_is_refused_before_download(world, monkeypatch):
    from src.services import file_conversion_service as module

    monkeypatch.setattr(module, "action_cap", lambda: 8)
    ref = _plant(world, "scan.pdf", b"%PDF-" + b"x" * 20, mime=PDF)
    calls_before = len(world.drive.calls)
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(ref, "ann", action="save")
    assert "cannot be saved to the drive" in raised.value.line
    assert not [c for c in world.drive.calls[calls_before:] if c[0] == "download"]


async def test_a_chat_file_above_the_cap_is_refused_on_its_bytes(world):
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file("big.bin", "ann", action="send")
    assert "cannot be sent to the chat" in raised.value.line


async def test_a_drive_folder_is_refused_with_the_list_hint(world):
    ref = _plant(world, "Receipts", b"", folder=True)
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(ref, "ann", action="send")
    assert raised.value.line == folder_note("Receipts")


async def test_an_unknown_action_is_a_programming_error(world):
    with pytest.raises(ValueError):
        await world.service.resolve_file("invoice_FA-0137.pdf", "ann", action="open")


# -- the email notes -----------------------------------------------------------------------------------------------

async def test_a_missing_part_is_the_missing_note(world):
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref(MSG, "9"), "ann", action="send")
    assert raised.value.line == ATTACHMENT_MISSING_NOTE


async def test_an_empty_part_is_the_empty_note(world):
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref(MSG, "2"), "ann", action="send")
    assert raised.value.line == ATTACHMENT_EMPTY_NOTE


async def test_a_gone_message_is_the_gone_note(world):
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref("msg_deleted", "1"), "ann", action="save")
    assert raised.value.line == EMAIL_GONE_NOTE


async def test_a_mailbox_that_does_not_answer_is_the_unavailable_note(world):
    world.mailbox.fail_with = EmailPartUnavailable("429")
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref(MSG, "1.2"), "ann", action="send")
    assert raised.value.line == MAILBOX_UNAVAILABLE_NOTE


async def test_no_mail_credential_is_the_not_connected_note(world):
    world.email.connected = False
    with pytest.raises(FileNotResolved) as raised:
        await world.service.resolve_file(format_email_ref(MSG, "1.2"), "ann", action="send")
    assert raised.value.line == EMAIL_NOT_CONNECTED_NOTE


async def test_without_an_email_service_it_is_not_configured(world):
    service = FileConversionService(storage=world.storage, access=allow_access())
    with pytest.raises(FileNotResolved) as raised:
        await service.resolve_file(format_email_ref(MSG, "1.2"), "ann", action="send")
    assert raised.value.line == EMAIL_OPEN_NOT_CONFIGURED_NOTE


async def test_an_email_ref_of_the_wrong_shape_is_not_found(world):
    with pytest.raises(FileNotFoundError):
        await world.service.resolve_file("email:no-part", "ann", action="send")


async def test_resolve_bytes_still_refuses_an_email_ref(world):
    with pytest.raises(EmailFileNotTransferable):
        await world.service.resolve_bytes(format_email_ref(MSG, "1.2"), "ann")
    assert world.email.downloads == []


# -- D13: active_kind_of ---------------------------------------------------------------------------------------------

async def test_a_pdf_is_sent_without_asking_the_sandbox(world):
    resolved = ResolvedFile(data=b"%PDF-1.7 quote", name="bathroom_quote.pdf", mime=PDF)
    assert await world.service.active_kind_of(resolved, deadline_at=DEADLINE) is None
    assert world.containers.calls == []


async def test_a_zip_head_asks_the_sandbox_and_its_macros_block(world):
    world.containers.facts = ContainerFacts(status=ContainerStatus.ZIP, members=3, vba_project=True)
    resolved = await world.service.resolve_file(format_email_ref(MSG, "1.3"), "ann", action="send")
    assert await world.service.active_kind_of(resolved, deadline_at=DEADLINE) == ak.OFFICE_MACROS
    assert world.containers.calls == [resolved.data]


async def test_a_plain_container_is_sent(world):
    resolved = ResolvedFile(data=zc.plain_docx(), name="bathroom_quote.docx", mime=DOCX)
    assert await world.service.active_kind_of(resolved, deadline_at=DEADLINE) is None


async def test_header_bytes_decide_without_the_sandbox(world):
    resolved = ResolvedFile(data=b"<!doctype html><html>pay</html>", name="invoice.txt", mime="text/plain")
    assert await world.service.active_kind_of(resolved, deadline_at=DEADLINE) == ak.HTML
    assert world.containers.calls == []


@pytest.mark.parametrize("containers", [
    FakeContainers(raises=ParserUnavailable("busy")),
    FakeContainers(raises=RuntimeError("boom")),
    FakeContainers(facts=ContainerFacts(status=ContainerStatus.ERROR, detail="timeout: deadline")),
    None,
])
async def test_an_unchecked_container_fails_closed(world, containers, caplog):
    world.service.attach_documents(RecordingDocuments(), containers=containers)
    resolved = ResolvedFile(data=zc.plain_docx(), name="bathroom_quote.docx", mime=DOCX)
    with caplog.at_level(logging.INFO):
        assert await world.service.active_kind_of(resolved, deadline_at=DEADLINE) == ak.UNCHECKED
    assert any(r.levelno >= logging.WARNING and "[FileResolve]" in r.getMessage() for r in caplog.records)
