"""The indexer's download caps per kind against the chat/open cap (decisions/drive_indexer_pdf_download_cap.md):
a PDF is admitted up to MAX_PDF_DOWNLOAD_BYTES by the indexer, every other document kind stays at the chat cap."""
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_index import DriveChange, Reason, RecordState, Scope
from src.domain.drive_index_params import MAX_DOWNLOAD_BYTES, MAX_PDF_DOWNLOAD_BYTES, MAX_SHEET_DOWNLOAD_BYTES
from src.domain.drive_index_planner import classify
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS, size_cap
from src.domain.user_drive import MAX_DRIVE_DOWNLOAD_BYTES

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)
BETWEEN = (MAX_DRIVE_DOWNLOAD_BYTES + MAX_PDF_DOWNLOAD_BYTES) // 2   # over the chat cap, under the indexer's PDF cap


@pytest.fixture(autouse=True)
def admitted(monkeypatch):
    monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
    monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)


def ch(name, size):
    return DriveChange(item_id="a", name=name, parent_id="root", size_bytes=size, content_version="c1",
                       modified_at=NOW - timedelta(days=1), fs_modified_at=NOW - timedelta(days=30))


def test_the_pdf_cap_is_above_the_chat_cap_and_the_others_equal_it():
    assert MAX_PDF_DOWNLOAD_BYTES > MAX_DRIVE_DOWNLOAD_BYTES
    assert MAX_DOWNLOAD_BYTES == MAX_DRIVE_DOWNLOAD_BYTES


def test_pdf_gets_the_indexer_pdf_cap():
    assert size_cap("pdf") == MAX_PDF_DOWNLOAD_BYTES


@pytest.mark.parametrize("kind", sorted(SLICE2_CONTENT_KINDS - {"pdf", "xlsx", "xls"}))
def test_every_other_content_kind_keeps_the_chat_cap(kind):
    assert size_cap(kind) == MAX_DOWNLOAD_BYTES


@pytest.mark.parametrize("kind", ["xlsx", "xls"])
def test_sheets_keep_their_own_cap(kind):
    assert size_cap(kind) == MAX_SHEET_DOWNLOAD_BYTES


def test_a_pdf_between_the_caps_is_admitted():
    assert classify(ch("large.pdf", BETWEEN), Scope.NORMAL, None, None).state is RecordState.PENDING


def test_a_pdf_at_its_cap_is_admitted():
    assert classify(ch("large.pdf", MAX_PDF_DOWNLOAD_BYTES), Scope.NORMAL, None, None).state is RecordState.PENDING


def test_a_pdf_over_its_cap_is_too_large():
    cls = classify(ch("large.pdf", MAX_PDF_DOWNLOAD_BYTES + 1), Scope.NORMAL, None, None)
    assert (cls.state, cls.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)


@pytest.mark.parametrize("name", ["large.docx", "large.epub", "large.txt", "large.pptx"])
def test_another_document_between_the_caps_is_too_large(name):
    cls = classify(ch(name, BETWEEN), Scope.NORMAL, None, None)
    assert (cls.state, cls.reason) == (RecordState.METADATA_ONLY, Reason.TOO_LARGE)
