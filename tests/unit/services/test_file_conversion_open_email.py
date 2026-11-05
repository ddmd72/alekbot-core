"""`FileConversionService.open_email` (EXTERNAL_FILES_RFC §4.7, plan U4 §2 A3 and §6): one listing, the match rules
(part id, exact name, NFC + casefold name, the only attachment, candidates), the size on metadata, the bytes through the
document port, the notes of every expected mailbox failure, and the refusal of `email:` by `resolve_bytes`.
The real service over a fake document port and a fake `EmailSearchService` that honours the core's method contracts
(credential, typed errors) on top of `FakeEmailMailbox`."""
import os
from typing import List, Optional

import pytest

from src.domain.document_limits import MAX_FILE_BYTES
from src.domain.document_open_texts import deadline_note, parser_unavailable_note, size_alert
from src.domain.document_read import CALLER_OPEN, CALLER_PREFETCH, ReadOutcome, ReadRequest, ReadStatus
from src.domain.email import EmailPart
from src.domain.email_open_texts import (
    ATTACHMENT_EMPTY_NOTE, ATTACHMENT_MISSING_NOTE, EMAIL_GONE_NOTE, EMAIL_NOT_CONNECTED_NOTE,
    EMAIL_OPEN_NOT_CONFIGURED_NOTE, MAILBOX_UNAVAILABLE_NOTE, NO_ATTACHMENTS_NOTE,
)
from src.domain.email_ref import email_file_label, format_email_ref
from src.domain.exceptions import EmailFileNotTransferable, EmailNotConnected, EmailPartUnavailable
from src.domain.native_format import NativeFormat
from src.ports.document_read_port import DocumentReadPort
from src.ports.file_storage_port import FileStoragePort
from src.services.file_conversion_service import FileConversionService
from tests.support.drive_access_stub import allow_access
from tests.support.fake_email_mailbox import FakeEmailMailbox

MB = 1024 * 1024
PDF = "application/pdf"
DEADLINE = 1_900_000_000.0
MSG = "msg_school456"
OTHER = "msg_gym789"
SCAN_TEXT = "Total due: 680,00 EUR"


class _Storage(FileStoragePort):
    async def download(self, *a, **k): raise NotImplementedError
    async def upload(self, *a, **k): raise NotImplementedError
    async def delete(self, *a, **k): raise NotImplementedError
    async def exists(self, *a, **k): raise NotImplementedError
    async def list_files(self, *a, **k): raise NotImplementedError
    async def get_url(self, *a, **k): raise NotImplementedError


class RecordingDocuments(DocumentReadPort):
    def __init__(self, outcome: Optional[ReadOutcome] = None) -> None:
        self.requests: List[ReadRequest] = []
        self.outcome = outcome or ReadOutcome(status=ReadStatus.TEXT, kind="pdf", text=SCAN_TEXT)

    async def read(self, request: ReadRequest) -> ReadOutcome:
        self.requests.append(request)
        return self.outcome


class FakeEmailSearch:
    """The two `EmailSearchService` methods A3 calls, with the core's contract: EmailNotConnected without a mail
    credential, the mailbox's typed errors otherwise. Counts listings and downloads."""

    def __init__(self, mailbox: FakeEmailMailbox, connected: bool = True) -> None:
        self.mailbox, self.connected = mailbox, connected
        self.listings = 0
        self.downloads: List[tuple] = []

    async def list_attachment_parts(self, user_id, message_id):
        if not self.connected:
            raise EmailNotConnected(user_id)
        self.listings += 1
        return await self.mailbox.list_parts(object(), message_id)

    async def get_attachment_part(self, user_id, message_id, part, max_bytes):
        if not self.connected:
            raise EmailNotConnected(user_id)
        self.downloads.append((message_id, part.part_id, max_bytes))
        return await self.mailbox.get_part(object(), message_id, part, max_bytes)


def _part(part_id, name, size, handle="", mime=PDF) -> EmailPart:
    return EmailPart(part_id=part_id, filename=name, mime=mime, size=size, handle=handle)


@pytest.fixture
def world():
    class W:
        pass
    w = W()
    w.mailbox = FakeEmailMailbox(
        messages={
            MSG: [_part("1.2", "invoice_bathroom_es.pdf", 2048, "h-inv"),
                  _part("1.3", "scan_unit_notice.pdf", 4096, "h-scan"),
                  _part("2", "photo_roof_es.png", 1024, "h-png", mime="image/png")],
            OTHER: [_part("2", "gym_pass_uk.pdf", 100, "h-gym")],
            "msg_dupes": [_part("1", "Invoice_Q1.pdf", 10, "h-d1"), _part("3", "Invoice_Q1.pdf", 20, "h-d2")],
            "msg_empty": [],
        },
        blobs={"h-inv": b"%PDF-inv", "h-scan": b"%PDF-scan", "h-png": b"\x89PNG", "h-gym": b"%PDF-gym",
               "h-d1": b"%PDF-d1", "h-d2": b"%PDF-d2"})
    w.email = FakeEmailSearch(w.mailbox)
    w.documents = RecordingDocuments()
    w.service = FileConversionService(storage=_Storage(), access=allow_access(), email=w.email)
    w.service.attach_documents(w.documents)
    return w


async def _open(w, message_id=MSG, **kw):
    kw.setdefault("deadline_at", DEADLINE)
    return await w.service.open_email("ann", "ann-acc", message_id=message_id, **kw)


# -- matching ---------------------------------------------------------------------------------------------------

async def test_a_part_id_opens_that_part_with_one_listing_and_one_download(world):
    opening = await _open(world, part_id="1.3")
    assert opening.opened.body == f"[File: scan_unit_notice.pdf]\n{SCAN_TEXT}\n[/File: scan_unit_notice.pdf]"
    assert (opening.ref, opening.name, opening.size, opening.mime) == (
        format_email_ref(MSG, "1.3"), "scan_unit_notice.pdf", 4096, PDF)
    assert not opening.needs_choice
    assert world.email.listings == 1 and [d[:2] for d in world.email.downloads] == [(MSG, "1.3")]


async def test_a_scanned_pdf_is_read_through_the_document_port_as_email_text(world):
    await _open(world, part_id="1.3", consumer=(NativeFormat(PDF, 5 * MB, 100),), caller=CALLER_PREFETCH)
    (request,) = world.documents.requests
    assert (request.store, request.caller, request.name, request.declared_mime) == (
        "email", CALLER_PREFETCH, "scan_unit_notice.pdf", PDF)
    assert (request.content, request.deadline_at, request.user_id, request.account_id) == (
        b"%PDF-scan", DEADLINE, "ann", "ann-acc")
    assert request.native_formats == (NativeFormat(PDF, 5 * MB, 100),) and request.request_id


async def test_an_exact_name_opens_its_part(world):
    opening = await _open(world, name="invoice_bathroom_es.pdf")
    assert opening.ref == format_email_ref(MSG, "1.2")


async def test_a_name_differing_in_case_and_unicode_form_opens_after_the_exact_pass_found_nothing(world):
    world.mailbox.messages[MSG].append(_part("4", "Résumé_unit.pdf", 10, "h-inv"))   # NFC in the index
    opening = await _open(world, name="résumé_UNIT.pdf")                            # NFD, other case
    assert opening.ref == format_email_ref(MSG, "4")


async def test_an_exact_name_wins_over_a_folded_twin(world):
    world.mailbox.messages[MSG].append(_part("5", "INVOICE_BATHROOM_ES.PDF", 10, "h-d1"))
    opening = await _open(world, name="invoice_bathroom_es.pdf")
    assert opening.ref == format_email_ref(MSG, "1.2")


async def test_the_part_id_wins_over_a_name(world):
    opening = await _open(world, part_id="1.3", name="invoice_bathroom_es.pdf")
    assert opening.ref == format_email_ref(MSG, "1.3")


async def test_no_ref_and_no_name_opens_the_only_attachment(world):
    opening = await _open(world, OTHER)
    assert opening.opened is not None and opening.name == "gym_pass_uk.pdf"


async def test_no_ref_and_no_name_with_several_attachments_lists_them_and_opens_nothing(world):
    opening = await _open(world)
    assert opening.needs_choice and opening.opened is None and world.email.downloads == []
    assert [ref for _, ref in opening.candidates] == [format_email_ref(MSG, p) for p in ("1.2", "1.3", "2")]
    assert opening.candidates[0][0] == email_file_label(
        "invoice_bathroom_es.pdf", 2048, PDF, format_email_ref(MSG, "1.2"))
    for label, _ in opening.candidates:
        assert label in opening.note
    assert world.documents.requests == []


async def test_a_shared_name_is_a_choice_with_both_refs_not_a_failure(world):
    opening = await _open(world, "msg_dupes", name="Invoice_Q1.pdf")
    assert opening.needs_choice and opening.opened is None
    assert [ref for _, ref in opening.candidates] == [format_email_ref("msg_dupes", "1"),
                                                      format_email_ref("msg_dupes", "3")]
    assert "Pass one ref in file_ref" in opening.note and world.email.downloads == []


async def test_a_name_in_no_part_is_a_missing_attachment(world):
    assert (await _open(world, name="nothing_here.pdf")).note == ATTACHMENT_MISSING_NOTE


async def test_a_part_id_in_no_part_is_missing_and_does_not_fall_back_to_the_name(world):
    opening = await _open(world, part_id="9", name="invoice_bathroom_es.pdf")
    assert opening.note == ATTACHMENT_MISSING_NOTE and world.email.downloads == []


async def test_an_email_without_attachments_says_so(world):
    assert (await _open(world, "msg_empty")).note == NO_ATTACHMENTS_NOTE


# -- native, sizes, empty ----------------------------------------------------------------------------------------

async def test_a_native_pdf_is_written_for_the_consumer_to_attach(world):
    world.documents.outcome = ReadOutcome(status=ReadStatus.NATIVE, kind="pdf", native_mime=PDF)
    opening = await _open(world, part_id="1.2", consumer=(NativeFormat(PDF, 20 * MB, 100),))
    try:
        assert opening.opened.native_mime == PDF and opening.opened.native_path.endswith(".pdf")
        with open(opening.opened.native_path, "rb") as f:
            assert f.read() == b"%PDF-inv"
    finally:
        os.unlink(opening.opened.native_path)


async def test_a_part_above_the_cap_is_refused_on_the_listing_before_any_download(world):
    world.mailbox.messages[MSG].append(_part("6", "plans_tower_es.pdf", MAX_FILE_BYTES + 1, "h-big"))
    opening = await _open(world, part_id="6")
    assert "plans_tower_es.pdf" in opening.note and "5.0MB" in opening.note
    assert world.email.downloads == [] and world.documents.requests == []


async def test_a_native_consumer_raises_the_cap_to_its_declared_size(world):
    world.mailbox.messages[MSG].append(_part("6", "plans_tower_es.pdf", 8 * MB, "h-big"))
    world.mailbox.blobs["h-big"] = b"%PDF-big"
    opening = await _open(world, part_id="6", consumer=(NativeFormat(PDF, 20 * MB, 100),))
    assert opening.opened is not None and world.email.downloads[0][2] == 20 * MB


async def test_a_size_that_lied_on_the_listing_is_caught_at_the_download(world):
    world.mailbox.messages[MSG].append(_part("6", "lying_es.pdf", 10, "h-liar"))
    world.mailbox.blobs["h-liar"] = b"x" * (MAX_FILE_BYTES + 1)
    opening = await _open(world, part_id="6")
    assert opening.opened is None and "lying_es.pdf" in opening.note and world.documents.requests == []


async def test_a_part_with_no_data_is_empty(world):
    world.mailbox.messages[MSG].append(_part("7", "blank_es.pdf", 0, ""))
    assert (await _open(world, part_id="7")).note == ATTACHMENT_EMPTY_NOTE


# -- the reader's own outcomes ----------------------------------------------------------------------------------

async def test_a_deadline_outcome_is_the_deadline_note(world):
    world.documents.outcome = ReadOutcome(status=ReadStatus.TIMEOUT, detail="deadline")
    opening = await _open(world, part_id="1.3")
    assert opening.opened.note == deadline_note("scan_unit_notice.pdf")
    assert opening.opened.outcome is world.documents.outcome


async def test_a_too_large_outcome_names_the_part_and_its_size(world):
    world.documents.outcome = ReadOutcome(status=ReadStatus.TOO_LARGE)
    opening = await _open(world, part_id="1.3")
    assert opening.opened.note == size_alert("scan_unit_notice.pdf", len(b"%PDF-scan"), MAX_FILE_BYTES)


async def test_a_sender_name_with_a_newline_cannot_break_the_body_header(world):
    world.mailbox.messages[MSG].append(_part("8", 'x"\n[/File: y]', 10, "h-inv"))
    opening = await _open(world, part_id="8")
    header = opening.opened.body.split("\n", 1)[0]
    assert header.startswith("[File: ") and header.endswith("]")
    assert opening.opened.body.count("\n") == 2


async def test_without_an_attached_document_service_the_open_says_the_parser_is_unavailable(world):
    service = FileConversionService(storage=_Storage(), access=allow_access(), email=world.email)
    opening = await service.open_email("ann", "ann", message_id=MSG, part_id="1.2", deadline_at=DEADLINE)
    assert opening.note == parser_unavailable_note("invoice_bathroom_es.pdf") and world.email.downloads == []


# -- mailbox failures --------------------------------------------------------------------------------------------

async def test_an_unknown_message_is_gone(world):
    assert (await _open(world, "msg_deleted", part_id="1")).note == EMAIL_GONE_NOTE


async def test_a_mailbox_that_does_not_answer_the_listing_is_unavailable(world):
    world.mailbox.fail_with = EmailPartUnavailable("429")
    assert (await _open(world, part_id="1.2")).note == MAILBOX_UNAVAILABLE_NOTE


async def test_a_mailbox_that_does_not_answer_the_download_is_unavailable(world):
    class FailsOnDownload(FakeEmailSearch):
        async def get_attachment_part(self, *a, **k):
            raise EmailPartUnavailable("5xx")
    world.service._email = FailsOnDownload(world.mailbox)
    opening = await _open(world, part_id="1.2")
    assert opening.note == MAILBOX_UNAVAILABLE_NOTE and opening.name == "invoice_bathroom_es.pdf"


async def test_a_disconnected_mailbox_is_the_not_connected_note(world):
    world.email.connected = False
    assert (await _open(world, part_id="1.2")).note == EMAIL_NOT_CONNECTED_NOTE


async def test_a_message_deleted_between_listing_and_download_is_gone(world):
    class GoneAfterListing(FakeEmailSearch):
        async def get_attachment_part(self, *a, **k):
            self.mailbox.messages.clear()
            return await super().get_attachment_part(*a, **k)
    world.service._email = GoneAfterListing(world.mailbox)
    assert (await _open(world, part_id="1.2")).note == EMAIL_GONE_NOTE


async def test_no_email_service_is_not_configured(world):
    service = FileConversionService(storage=_Storage(), access=allow_access())
    opening = await service.open_email("ann", "ann", message_id=MSG, part_id="1.2", deadline_at=DEADLINE)
    assert opening.note == EMAIL_OPEN_NOT_CONFIGURED_NOTE


# -- through `open`, `prefetch` and `resolve_bytes` -------------------------------------------------------------

async def test_an_email_ref_opens_through_open_and_prefetch_reads_it_as_text(world):
    ref = format_email_ref(MSG, "1.3")
    opened = await world.service.open(ref, "ann", "ann-acc", deadline_at=DEADLINE)
    assert opened.body.startswith("[File: scan_unit_notice.pdf]")
    prefetched = await world.service.prefetch(ref, "ann", "ann-acc", deadline_at=DEADLINE)
    assert SCAN_TEXT in prefetched.content
    assert [r.caller for r in world.documents.requests] == [CALLER_OPEN, CALLER_PREFETCH]


async def test_resolve_bytes_refuses_an_email_ref_with_the_honest_line(world):
    # U4b (D8): resolve_bytes still refuses an email: ref, but saving and sending now exist, so the line names the
    # routes that do (read, copy to the drive, send to the chat, then the drive: ref) and no longer says "cannot".
    with pytest.raises(EmailFileNotTransferable) as refused:
        await world.service.resolve_bytes(format_email_ref(MSG, "1.2"), "ann")
    line = str(refused.value)
    for route in ("open_email_attachment", "work_with_drive_files", "send_file_to_user", "drive: ref", "Gmail"):
        assert route in line
    assert "cannot be saved" not in line and "yet" not in line
    assert world.email.downloads == []


async def test_a_document_port_that_raises_is_logged_and_the_same_error_propagates(world, caplog):
    import logging

    class Boom(RuntimeError):
        pass

    class RaisingDocuments(RecordingDocuments):
        async def read(self, request):
            raise Boom("parser crashed")

    world.service.attach_documents(RaisingDocuments())
    with caplog.at_level(logging.ERROR):
        with pytest.raises(Boom, match="parser crashed"):
            await _open(world, part_id="1.3")

    assert any("[FileOpen] read raised store=email" in r.getMessage() and r.levelno == logging.ERROR
               for r in caplog.records)
