"""Card text v3 (USER_DRIVE_INDEXING_RFC §25.2.7, CARD_VERSION 3): the profile and photo fields; lead text only for
machine text; a collapsed folder's README profile."""
from datetime import date

from src.domain.drive_card import card_text, collapsed_folder
from src.domain.drive_enrichment import DriveProfile, PhotoObject, ProfileDate, ProfileEntity
from src.domain.drive_index import DocumentBrief, DriveIndexRecord, Reason, RecordState, Scope, TriageVerdict

PROFILE = DriveProfile(title="Colosseum entry", what_it_is="Two entry tickets to the Colosseum", language="en",
                       doc_type="ticket", dates=(ProfileDate("event", date(2026, 5, 12)),),
                       entities=(ProfileEntity("Rome", "place", "Rome"), ProfileEntity("Colosseum", "place",
                                                                                       "Colosseum")),
                       topics=("travel", "sightseeing"), topics_native=("viaje",))


def rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="entry.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED)
    base.update(kw)
    return DriveIndexRecord(**base)


def test_a_profiled_file():
    assert card_text(rec(profile=PROFILE)) == ("entry.pdf | PDF document | ticket | Two entry tickets to the Colosseum"
                                               " | Colosseum entry | Rome, Colosseum | travel, sightseeing | viaje")


def test_a_profiled_file_never_carries_lead_text():
    assert "LEAD" not in card_text(rec(profile=PROFILE, lead_text="LEAD TEXT"))


def test_a_text_file_without_a_profile_keeps_the_readers_brief():
    brief = DocumentBrief(doc_type="receipt", description="a receipt")
    # CARD_VERSION 4 (Δ64): the brief's doc type is not on the card
    assert card_text(rec(brief=brief, lead_text="LEAD")) == "entry.pdf | PDF document | a receipt"


def test_machine_text_keeps_its_lead_text():
    text = card_text(rec(name="a.log.txt", kind="text", state=RecordState.METADATA_ONLY, reason=Reason.MACHINE_TEXT,
                         lead_text="2026-10-01 INFO"))
    assert text == "a.log.txt | text file | machine-generated text | 2026-10-01 INFO"


def test_a_photograph():
    photo = rec(name="IMG_1.jpg", kind="image", state=RecordState.METADATA_ONLY, reason=Reason.PHOTOGRAPH,
                triage_verdict=TriageVerdict.PHOTO, photo_description="a square with a fountain at night",
                photo_scene="city square", photo_objects=(PhotoObject("fountain"), PhotoObject("church")),
                visible_text="BAR 24", place_name="Rome (Roma), Italy")
    assert card_text(photo) == ("IMG_1.jpg | image | a square with a fountain at night | city square | "
                                "fountain, church | BAR 24 | place Rome (Roma), Italy")


def test_a_collapsed_folder_with_and_without_a_readme_profile():
    folder = rec(name="tool", kind="folder", is_folder=True, scope=Scope.OPAQUE_ROOT,
                 state=RecordState.METADATA_ONLY, reason=Reason.OPAQUE_ROOT)
    assert collapsed_folder(folder) and card_text(folder) == "tool | repository"
    p = DriveProfile(title="Invoice tool", what_it_is="Converts invoices to CSV", language="en", doc_type="other",
                     topics=("invoices",))
    assert card_text(rec(**{**folder.__dict__, "profile": p})) == (
        "tool | repository | Converts invoices to CSV | Invoice tool | invoices")
    assert not collapsed_folder(rec(name="docs", kind="folder", is_folder=True, reason=Reason.FOLDER))
