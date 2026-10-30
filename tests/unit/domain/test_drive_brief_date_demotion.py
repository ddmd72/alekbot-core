"""A demoted reader file's brief date is graded against the read text before it dates the file
(USER_DRIVE_INDEXING_RFC §26.4, Δ60 on the brief path)."""
from datetime import datetime, timezone

from src.domain.drive_extraction import ExtractionOutcome, ExtractionStatus
from src.domain.drive_index import DocumentBrief, DriveIndexRecord, Reason, RecordState, Scope
from src.domain.drive_index_planner import demoted

NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
BRIEF_DATE = datetime(2024, 3, 15, tzinfo=timezone.utc)


def rec() -> DriveIndexRecord:
    return DriveIndexRecord(user_id="u", connection_id="u", item_id="x", name="a.pdf", kind="pdf", scope=Scope.NORMAL,
                            state=RecordState.PENDING, content_version="c1")


def outcome(text: str) -> ExtractionOutcome:
    return ExtractionOutcome(status=ExtractionStatus.TEXT, text=text, extracted_by="reader@2",
                             brief=DocumentBrief(description="a receipt", doc_date=BRIEF_DATE))


def test_a_brief_date_the_text_holds_dates_the_demoted_file():
    out = demoted(rec(), Reason.USER_CHUNK_CAP, NOW, outcome=outcome("Receipt of 15.03.2024, total 12,00"),
                  chunk_count=3)
    assert out.brief is not None and out.brief.date_grade == "in_text"
    assert out.effective_date == BRIEF_DATE


def test_a_brief_date_the_text_lacks_never_dates_the_demoted_file():
    out = demoted(rec(), Reason.USER_CHUNK_CAP, NOW, outcome=outcome("Receipt, total 12,00 " * 20), chunk_count=3)
    assert out.brief is not None and out.brief.date_grade == "not_in_text" and out.brief.held_date is None
    assert out.effective_date != BRIEF_DATE
