"""Code-written notes in tool results speak as `[Tool note: …]`, never as the system (SYSTEM_TURN_FINGERPRINT_RFC §4.5)."""
import pytest

from src.domain import document_open_texts as doc
from src.domain import email_open_texts as mail
from src.domain.document_limits import MAX_FILE_BYTES
from src.domain.document_read import ReadOutcome, ReadStatus

NAME = "R&D/Tom's plan.docx"
CHAT_FILE = "birth-certificate.pdf"
INVOICE = "FA-0137"
PAGE_OUTCOME = ReadOutcome(status=ReadStatus.TEXT, pages_total=4, pages_read=2, skipped_pages=(3,))

_DOC_NOTES = [
    doc.size_alert(INVOICE, 2 * MAX_FILE_BYTES),
    doc.conversion_alert(CHAT_FILE, "application/pdf"),
    doc.conversion_alert(CHAT_FILE),
    doc.audio_unavailable_note("voice.mp3"),
    doc.cut_note(CHAT_FILE, 1000, 500),
    doc.still_working_note(NAME),
    doc.deadline_note(NAME),
    doc.model_unavailable_note(NAME),
    doc.parser_unavailable_note(NAME),
    doc.image_too_large_note("photo.png"),
    doc.image_format_note("photo.bmp"),
    doc.open_failed_note("drive:main:abc"),
    doc.storage_read_note("drive:main:abc"),
    doc.folder_note("R&D"),
    doc.folder_note(""),
    doc.action_too_large_note(NAME, 3 * MAX_FILE_BYTES, MAX_FILE_BYTES, "save"),
    doc.action_too_large_note(NAME, 3 * MAX_FILE_BYTES, MAX_FILE_BYTES, "send"),
    doc.shown_line(NAME, PAGE_OUTCOME),
]

_EMAIL_NOTES = [
    mail.EMAIL_GONE_NOTE,
    mail.ATTACHMENT_MISSING_NOTE,
    mail.ATTACHMENT_EMPTY_NOTE,
    mail.MAILBOX_UNAVAILABLE_NOTE,
    mail.NO_ATTACHMENTS_NOTE,
    mail.EMAIL_OPEN_NOT_CONFIGURED_NOTE,
    mail.EMAIL_FILE_NOT_TRANSFERABLE_NOTE,
    mail.active_kind_refusal_note("unchecked"),
    mail.active_kind_refusal_note("macro_office"),
    mail.choose_one_note([f"{INVOICE}.pdf (ref a)", f"{INVOICE}.pdf (ref b)"], f"{INVOICE}.pdf"),
    mail.choose_one_note(["one", "two"]),
]


@pytest.mark.parametrize("note", _DOC_NOTES + _EMAIL_NOTES)
def test_note_starts_with_tool_note_and_never_claims_the_system(note):
    assert note.startswith("[Tool note:")
    assert "[System:" not in note
    assert "SYSTEM" not in note
