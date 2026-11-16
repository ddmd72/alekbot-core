"""EmailAttachment after slice 5: a read-by-reader attachment carries text, and the reader fields are in-memory
counters that cannot be negative. The stored `outcome` value is unchanged."""
import pytest

from src.domain.email_attachment import AttachmentRead, EmailAttachment


def test_a_reader_read_accepts_text_and_reports_its_outcome():
    a = EmailAttachment(name="LAB-2025-03-0417.pdf", kind="pdf", size=9000, read=AttachmentRead.READ_BY_READER,
                        text="All values in range.", head="All values in range.", reader_pages=3, reader_usd=0.002)
    assert a.outcome == "read_by_reader" and a.reader_pages == 3 and a.reader_usd == 0.002


def test_the_reader_fields_default_to_zero():
    a = EmailAttachment(name="RM-55120.pdf", kind="pdf", size=9000, read=AttachmentRead.TEXT, text="x")
    assert (a.reader_pages, a.reader_usd) == (0, 0.0)


@pytest.mark.parametrize("fields", [{"reader_pages": -1}, {"reader_usd": -0.01}])
def test_negative_reader_fields_are_refused(fields):
    with pytest.raises(ValueError):
        EmailAttachment(name="a.pdf", kind="pdf", size=1, read=AttachmentRead.SCAN_NOT_READ, **fields)


@pytest.mark.parametrize("read", [AttachmentRead.SCAN_NOT_READ, AttachmentRead.LOCKED])
def test_text_stays_refused_for_a_file_that_was_not_read(read):
    with pytest.raises(ValueError):
        EmailAttachment(name="a.pdf", kind="pdf", size=1, read=read, text="x")


def test_the_outcome_of_the_other_reads_is_unchanged():
    assert EmailAttachment(name="a.pdf", kind="pdf", size=1, read=AttachmentRead.SKIPPED, reason="no_text",
                           reader_pages=2).outcome == "skipped:no_text"
    assert EmailAttachment(name="a.pdf", kind="pdf", size=1, read=AttachmentRead.SCAN_NOT_READ).outcome == "scan_not_read"
