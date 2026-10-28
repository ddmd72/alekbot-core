"""The owner's note in the card (DRIVE_FILE_NOTES_RFC §5.3, D7, G5): a record without a note keeps a byte-identical card
text and hash in every branch (no CARD_VERSION bump); with a note the segment sits right after `name | kind` (a
collapsed folder: after its label), a classifier-only note never shows an empty quote, the profile's doc type is never
shown twice, and `owner=False` leaves the segment out for the renderer. Synthetic records only."""
from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from src.domain.drive_card import card_hash, card_text, note_line, owner_note_line, owner_segment
from src.domain.drive_enrichment import DriveProfile, PhotoObject
from src.domain.drive_index import (
    CARD_VERSION, DocumentBrief, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict,
)

T = datetime(2026, 3, 15, 10, tzinfo=timezone.utc)


def _rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="i1", name="Bathroom.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED)
    base.update(kw)
    return DriveIndexRecord(**base)


# One fixture per card_text branch, with the text the card had before the owner note existed (the G5 pin).
BRANCHES = {
    "profiled": (_rec(profile=DriveProfile("Repair", "Bathroom repair works", "en", "invoice", topics=("bathroom",)),
                      capture="scan", capture_source="detected"),
                 "Bathroom.pdf | PDF document | scan | invoice | Bathroom repair works | Repair | bathroom"),
    "unprofiled": (_rec(kind="docx", name="Notes.docx", brief=DocumentBrief(description="Meeting notes")),
                   "Notes.docx | Word document | Meeting notes"),
    "machine_text": (_rec(kind="log", name="run.log", reason=Reason.MACHINE_TEXT, lead_text="ERROR 42"),
                     "run.log | log file | machine-generated text | ERROR 42"),
    "photo": (_rec(kind="image", name="IMG_1.jpg", triage_verdict=TriageVerdict.PHOTO,
                   photo_description="a bathroom", photo_objects=(PhotoObject("sink"),)),
              "IMG_1.jpg | image | a bathroom | sink"),
    "recording": (_rec(kind="audio", name="call.m4a", transcript_state="current", transcript_seconds=600),
                  "call.m4a | audio | transcript: 10 min"),
    "collapsed_folder": (_rec(kind="folder", name="proj", is_folder=True, scope=Scope.OPAQUE_ROOT,
                              reason=Reason.OPAQUE_ROOT,
                              profile=DriveProfile("Proj", "A repository", "en", "other", topics=("code",))),
                         "proj | repository | A repository | Proj | code"),
    "plain_folder": (_rec(kind="folder", name="Bathroom", is_folder=True), "Bathroom | folder"),
}


@pytest.mark.parametrize("branch", sorted(BRANCHES))
def test_no_note_keeps_the_card_byte_identical(branch):
    rec, expected = BRANCHES[branch]
    assert card_text(rec) == expected
    assert card_text(rec, owner=False) == expected
    assert card_hash(rec) == hashlib.sha256(f"{CARD_VERSION}\n{expected}".encode("utf-8")).hexdigest()[:32]
    assert owner_segment(rec) == "" and note_line(rec) == ""


def test_an_empty_name_folder_keeps_its_slice_1_text():
    rec = _rec(kind="folder", name="", is_folder=True)
    assert card_text(rec) == " | folder"


NOTED = dict(owner_note_id="n1", owner_note_version=2, owner_note_text="the bathroom repair by Reformas Exemplo",
             owner_doc_type="contract", owner_life_areas=("home",), owner_note_merge_version=1)
SEGMENT = "noted by the owner: the bathroom repair by Reformas Exemplo; type contract; life areas home"


@pytest.mark.parametrize("branch,expected", [
    ("profiled", f"Bathroom.pdf | PDF document | {SEGMENT} | scan | Bathroom repair works | Repair | bathroom"),
    ("unprofiled", f"Notes.docx | Word document | {SEGMENT} | Meeting notes"),
    ("machine_text", f"run.log | log file | {SEGMENT} | machine-generated text | ERROR 42"),
    ("photo", f"IMG_1.jpg | image | {SEGMENT} | a bathroom | sink"),
    ("recording", f"call.m4a | audio | {SEGMENT} | transcript: 10 min"),
    ("collapsed_folder", f"proj | repository | {SEGMENT} | A repository | Proj | code"),
    ("plain_folder", f"Bathroom | folder | {SEGMENT}"),
])
def test_the_segment_follows_name_and_kind_in_every_branch(branch, expected):
    rec = replace(BRANCHES[branch][0], **NOTED)
    assert card_text(rec) == expected
    assert card_hash(rec) != card_hash(BRANCHES[branch][0])


def test_owner_false_leaves_the_segment_out_and_keeps_the_type_drop():
    rec = replace(BRANCHES["profiled"][0], **NOTED)
    assert card_text(rec, owner=False) == "Bathroom.pdf | PDF document | scan | Bathroom repair works | Repair | bathroom"


def test_the_profile_type_is_shown_when_the_owner_gave_none():
    rec = replace(BRANCHES["profiled"][0], owner_note_id="n1", owner_note_text="from Reformas Exemplo", owner_note_version=1)
    assert card_text(rec) == ("Bathroom.pdf | PDF document | noted by the owner: from Reformas Exemplo | scan | invoice | "
                              "Bathroom repair works | Repair | bathroom")


def test_the_type_is_never_shown_twice():
    rec = replace(BRANCHES["profiled"][0], **NOTED)
    assert card_text(rec).count("contract") == 1 and "invoice" not in card_text(rec)


def test_a_classifier_only_note_has_no_empty_quote():
    rec = _rec(owner_note_id="n1", owner_note_version=1, owner_doc_type="contract")
    assert owner_segment(rec) == "noted by the owner: type contract"
    assert note_line(rec) == "noted by the owner: type contract"
    areas = _rec(owner_note_id="n1", owner_note_version=1, owner_life_areas=("home", "finance"))
    assert note_line(areas) == "noted by the owner: life areas home, finance"


def test_the_rendered_line():
    assert owner_note_line("the bathroom repair invoice", "contract", ("home",)) == \
        "noted by the owner: «the bathroom repair invoice» (type contract; life areas home)"
    assert owner_note_line("the bathroom repair invoice", "", ()) == "noted by the owner: «the bathroom repair invoice»"
    assert owner_note_line("", "", ()) == ""


def test_labels_read_as_words():
    rec = _rec(owner_note_id="n1", owner_note_version=1, owner_doc_type="bank_statement",
               owner_life_areas=("legal_admin",))
    assert owner_segment(rec) == "noted by the owner: type bank statement; life areas legal admin"
    assert owner_note_line("from the bank", "bank_statement", ("legal_admin",)) == \
        "noted by the owner: «from the bank» (type bank statement; life areas legal admin)"
