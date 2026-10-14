"""A document dated by its profile cites "written in the document" (USER_DRIVE_INDEXING_RFC §25.4.1, D13 extended):
the profile's verified `issued` date is the record's effective date, so its card must not say "file date"."""
from datetime import date, datetime, timezone

from src.domain.drive_enrichment import DriveProfile, ProfileDate
from src.domain.drive_index import DriveIndexRecord, RecordState, Scope
from src.domain.drive_search import DATE_FILE, DATE_WRITTEN, date_source


def _rec(**kw) -> DriveIndexRecord:
    base = dict(user_id="u", item_id="x", name="invoice.docx", kind="docx", scope=Scope.NORMAL,
                state=RecordState.INDEXED)
    base.update(kw)
    return DriveIndexRecord(**base)


def _profile(issued: date) -> DriveProfile:
    return DriveProfile(title="Invoice", what_it_is="A repair invoice", language="en", doc_type="invoice",
                        dates=(ProfileDate("issued", issued),))


def test_a_profile_dated_document_cites_the_document():
    when = datetime(2024, 3, 7, tzinfo=timezone.utc)
    assert date_source(_rec(profile=_profile(date(2024, 3, 7)), effective_date=when)) == DATE_WRITTEN


def test_a_stale_profile_date_is_never_cited():
    when = datetime(2025, 1, 2, tzinfo=timezone.utc)
    assert date_source(_rec(profile=_profile(date(2024, 3, 7)), effective_date=when)) == DATE_FILE


def test_a_profile_without_an_issued_date_falls_back_to_the_file_date():
    profile = DriveProfile(title="Notes", what_it_is="Meeting notes", language="en", doc_type="note",
                           dates=(ProfileDate("event", date(2024, 3, 7)),))
    when = datetime(2024, 3, 7, tzinfo=timezone.utc)
    assert date_source(_rec(profile=profile, effective_date=when)) == DATE_FILE
