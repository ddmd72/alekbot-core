"""The shared receipt renderer (DRIVE_FILE_NOTES_RFC §4.6, §4.8, §4.9; core contract §6): the stored text byte for byte,
a classifier-only note with no empty quote, the delete line, every searchability status (archived and other-drive
included), the erase request with the command alone, the partial-erase counts, and localized headers in four
languages."""
import pytest

from src.domain.drive_note import (
    DRIVE_NOTE_ERASE_DELIVERY, DRIVE_NOTE_RECEIPT_DELIVERY, ERASE_SCOPE_ALL, ERASE_SCOPE_ONE, STATUS_ARCHIVED,
    STATUS_INDEXING_OFF, STATUS_NO_CARD, STATUS_NOT_INDEXED, STATUS_OTHER_DRIVE, STATUS_SOON, EraseRequestReceipt,
    NoteReceipt,
)
from src.domain.drive_note_receipt import (
    drive_note_call_event, render_drive_note_erase, render_drive_note_erased, render_drive_note_receipt,
)
from src.domain.ui_messages import UIMessage
from src.locales import en, es, fr, uk

TEXT = "the contractor's invoice {for} the bathroom repair — «ремонт ванної» 1.048,45 €"


def strings(module=en):
    return lambda message: module.UI_STRINGS[message.value]


def receipt(**kw) -> dict:
    base = dict(action="set", note_id="n1", ref="drive:abc", path="Docs/invoice.pdf", version=3, text=TEXT,
                doc_type="bank_statement", life_areas=("home", "legal_admin"), status=STATUS_SOON)
    base.update(kw)
    return NoteReceipt(**base).delivery_data()


def test_a_save_shows_the_header_the_stored_text_byte_for_byte_the_classifiers_status_and_undo():
    text = render_drive_note_receipt(receipt(), strings())
    lines = text.split("\n")
    assert lines[0] == "📝 Note saved on Docs/invoice.pdf (version 3):"
    assert lines[1] == f"«{TEXT}»"
    assert lines[2] == "type: bank statement · life areas: home, legal admin"
    assert lines[3] == en.UI_STRINGS[UIMessage.DRIVE_NOTE_STATUS_SOON.value]
    assert lines[4] == en.UI_STRINGS[UIMessage.DRIVE_NOTE_UNDO_HINT.value]


def test_a_classifier_only_note_has_no_empty_quote():
    text = render_drive_note_receipt(receipt(text="", doc_type="contract", life_areas=()), strings())
    assert "«" not in text and "»" not in text
    assert "type: contract" in text and "life areas" not in text


@pytest.mark.parametrize("status,message", [
    (STATUS_SOON, UIMessage.DRIVE_NOTE_STATUS_SOON),
    (STATUS_INDEXING_OFF, UIMessage.DRIVE_NOTE_STATUS_INDEXING_OFF),
    (STATUS_NOT_INDEXED, UIMessage.DRIVE_NOTE_STATUS_NOT_INDEXED),
    (STATUS_NO_CARD, UIMessage.DRIVE_NOTE_STATUS_NO_CARD),
    (STATUS_ARCHIVED, UIMessage.DRIVE_NOTE_STATUS_ARCHIVED),
    (STATUS_OTHER_DRIVE, UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE),
])
def test_every_status_has_its_line(status, message):
    assert en.UI_STRINGS[message.value] in render_drive_note_receipt(receipt(status=status), strings())


def test_the_two_statuses_the_ruling_added_are_worded_as_the_core_requires():
    assert "only a search of deleted files" in en.UI_STRINGS[UIMessage.DRIVE_NOTE_STATUS_ARCHIVED.value]
    assert "Its drive isn't connected" in en.UI_STRINGS[UIMessage.DRIVE_NOTE_STATUS_OTHER_DRIVE.value]


def test_an_identical_write_says_nothing_changed_and_offers_no_undo():
    text = render_drive_note_receipt(receipt(noop=True), strings())
    assert text.startswith("📝 The note on Docs/invoice.pdf already says exactly this (version 3)")
    assert f"«{TEXT}»" in text
    assert en.UI_STRINGS[UIMessage.DRIVE_NOTE_UNDO_HINT.value] not in text


def test_a_delete_says_history_kept_and_undo_possible():
    text = render_drive_note_receipt(receipt(action="clear", text="", doc_type="", life_areas=(), status="",
                                             version=4), strings())
    assert text.startswith("🗑️ Note removed from Docs/invoice.pdf.")
    assert "history is kept, so undo is possible" in text and "erase the history" in text


def test_a_delete_with_nothing_to_delete_names_the_ref_when_there_is_no_path():
    data = NoteReceipt(action="clear", note_id="", ref="drive:zz", path="", version=0, noop=True).delivery_data()
    assert render_drive_note_receipt(data, strings()) == "There was no note on drive:zz; nothing changed."


def test_undo_redo_undo_to_empty_and_a_repeated_undo():
    restored = render_drive_note_receipt(receipt(action="restore", status=""), strings())
    assert restored.startswith("↩️ Previous note put back on Docs/invoice.pdf (version 3):") and f"«{TEXT}»" in restored
    redone = render_drive_note_receipt(receipt(action="restore", status="", redo=True), strings())
    assert redone.startswith("↪️ The undone change is back")
    empty = render_drive_note_receipt(receipt(action="restore", status="", text="", doc_type="", life_areas=()),
                                      strings())
    assert empty.startswith("↩️ Undone on Docs/invoice.pdf: there was no note before") and "«" not in empty
    repeated = render_drive_note_receipt(receipt(action="restore", status="", noop=True), strings())
    assert repeated.startswith("↩️ Already undone") and en.UI_STRINGS[UIMessage.DRIVE_NOTE_UNDO_HINT.value] not in repeated


def test_a_note_without_a_path_is_named_by_its_note_ref():
    data = receipt(path="", ref="")
    assert render_drive_note_receipt(data, strings()).startswith("📝 Note saved on drive_note:n1 (version 3):")


@pytest.mark.parametrize("module", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_headers_are_localized_and_the_text_is_never_translated(module):
    text = render_drive_note_receipt(receipt(), strings(module))
    header = module.UI_STRINGS[UIMessage.DRIVE_NOTE_SAVED.value].format(path="Docs/invoice.pdf", version=3)
    assert text.split("\n")[0] == header
    assert f"«{TEXT}»" in text


def test_the_erase_request_is_the_line_and_the_command_alone():
    one = EraseRequestReceipt(scope=ERASE_SCOPE_ONE, code="a1b2c3", note_count=1, path="Docs/invoice.pdf",
                              version_count=4).delivery_data()
    line, command = render_drive_note_erase(one, strings())
    assert "Docs/invoice.pdf" in line and "versions: 4" in line and "10 minutes" in line and "a1b2c3" not in line
    assert command == "`$drive-notes erase a1b2c3`"
    every = EraseRequestReceipt(scope=ERASE_SCOPE_ALL, code="ffee01", note_count=7).delivery_data()
    line, command = render_drive_note_erase(every, strings())
    assert "notes: 7" in line and command == "`$drive-notes erase-all ffee01`"


def test_the_erase_reply_names_the_counts_left():
    assert render_drive_note_erased(3, 0, strings()) == "🗑️ Notes erased for good, with their history: 3."
    partial = render_drive_note_erased(2, 1, strings())
    # final review C-I3: the one-note case in the singular; an erase-all's partial names the command
    assert partial == "🗑️ Notes erased for good: 2. Not erased right now: 1; ask me to erase it again."
    assert render_drive_note_erased(2, 1, strings(), "all") == (
        "🗑️ Notes erased for good: 2. Not erased right now: 1; send `$drive-notes erase-all` again to erase them.")


@pytest.mark.parametrize("module", [uk, en, fr, es], ids=["uk", "en", "fr", "es"])
def test_the_erase_counts_read_right_for_one_note(module):
    one = render_drive_note_erased(1, 0, strings(module))
    assert one.rstrip(".").endswith(": 1")                  # "<label>: {count}", never "1 notes"
    partial = render_drive_note_erased(1, 1, strings(module))
    assert ": 1" in partial and "{" not in partial


def test_the_call_event_carries_the_ref_and_never_the_text_or_the_code():
    event = drive_note_call_event(DRIVE_NOTE_RECEIPT_DELIVERY, receipt())
    assert event == "[System: drive note saved during a call with Lelik: Docs/invoice.pdf ref=drive:abc v3]"
    assert TEXT not in event
    erase = EraseRequestReceipt(scope=ERASE_SCOPE_ONE, code="a1b2c3", note_count=1, path="Docs/x.pdf",
                                version_count=2).delivery_data()
    erase_event = drive_note_call_event(DRIVE_NOTE_ERASE_DELIVERY, erase)
    assert "Docs/x.pdf" in erase_event and "a1b2c3" not in erase_event
