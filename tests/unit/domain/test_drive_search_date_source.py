"""Each card's date says where it comes from (USER_DRIVE_INDEXING_RFC §10.2, D13, final review F5-3).

The effective date is a photo's taken date, a read document's own date (its brief), else the file's date. Smart cites
"the document's date": an unlabelled `date:` let it cite a file date as one written in the document."""
from datetime import datetime, timezone

from src.domain.drive_media_facts import DriveMediaFacts
from src.domain.drive_index import DocumentBrief, DriveIndexRecord, RecordState, Scope
from src.domain.drive_search import (
    DATE_FILE, DATE_TAKEN, DATE_WRITTEN, STATUS_OK, DriveSearchEnvelope, DriveSearchRequest, DriveSearchResult,
    date_source, make_card, render_results,
)

D1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
D2 = datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc)


def rec(**kw):
    base = dict(user_id="u", item_id="f1", name="a.pdf", kind="pdf", scope=Scope.NORMAL,
                state=RecordState.INDEXED, has_card=True, effective_date=D2, fs_modified_at=D2)
    return DriveIndexRecord(**{**base, **kw})


def test_the_source_of_each_date():
    assert date_source(rec()) == DATE_FILE
    assert date_source(rec(brief=DocumentBrief(doc_date=D1), effective_date=D1)) == DATE_WRITTEN
    taken = DriveMediaFacts(taken_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc))
    assert date_source(rec(kind="image", media=taken)) == DATE_TAKEN
    assert date_source(rec(effective_date=None)) is None


def test_a_brief_whose_date_is_not_the_effective_one_is_not_cited_as_written():
    assert date_source(rec(brief=DocumentBrief(doc_date=D1))) == DATE_FILE


def _render(r):
    card = make_card(r, "Docs/a.pdf", "a.pdf | PDF document", "n0000001", "a1")
    return render_results(DriveSearchResult(status=STATUS_OK, cards=(card,), request=DriveSearchRequest(query="a"),
                                            envelope=DriveSearchEnvelope(index_as_of=D2)))


def test_the_rendered_date_carries_its_source():
    assert "date: 2026-05-09 (file date)" in _render(rec())
    assert "date: 2026-03-01 (written in the document)" in _render(
        rec(brief=DocumentBrief(doc_date=D1), effective_date=D1))
    assert "date: 2026-05-09 (taken)" in _render(rec(kind="image", media=DriveMediaFacts(
        taken_at=datetime(2026, 5, 9, 10, 0, tzinfo=timezone.utc))))
    assert "date: unknown" in _render(rec(effective_date=None))
