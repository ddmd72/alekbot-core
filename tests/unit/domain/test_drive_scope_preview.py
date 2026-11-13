"""The scope-add preview's rules (decisions/drive_scope_add_preview.md): a file's bucket follows the indexer's own
admission rules, a folder is descended where the indexer descends, and `large` turns on at the threshold."""
import pytest

from src.domain.drive_index import DriveChange
from src.domain.drive_index_policy import size_cap
from src.domain.drive_scope_preview import (
    PREVIEW_LARGE_FILES, PreviewBucket, ScopePreview, bucket_of, descend_into, is_marked_opaque,
)


def _file(name: str, size: int = 10, mime: str = "") -> DriveChange:
    return DriveChange(item_id="f-1", name=name, size_bytes=size, mime_type=mime)


def _folder(name: str) -> DriveChange:
    return DriveChange(item_id="d-1", name=name, is_folder=True)


@pytest.mark.parametrize("change, bucket", [
    (_file("lease_porto.pdf"), PreviewBucket.DOCUMENT),
    (_file("Tom's plan.docx"), PreviewBucket.DOCUMENT),
    (_file("wa_dentist_ru.jpg"), PreviewBucket.IMAGE),
    (_file("audio-1.m4a"), PreviewBucket.RECORDING),
    (_file("archive.bin"), PreviewBucket.OTHER),
    (_file(".hidden.pdf"), PreviewBucket.OTHER),          # a dot name is never read, whatever its kind
    (_file("id_rsa"), PreviewBucket.OTHER),               # a secret name
])
def test_a_file_lands_in_the_bucket_the_indexer_would_give_it(change, bucket):
    assert bucket_of(change) is bucket


def test_a_document_over_its_download_cap_is_not_read():
    assert bucket_of(_file("scan_0012.pdf", size=size_cap("pdf") + 1)) is PreviewBucket.OTHER
    assert bucket_of(_file("scan_0012.pdf", size=size_cap("pdf"))) is PreviewBucket.DOCUMENT


def test_a_google_native_document_has_no_size_and_is_still_a_document():
    # the Google adapter reports a native with the mime of its export (google_drive_mapping.EXPORT_MIMES) and no size
    assert bucket_of(_file("Tom's plan", size=0, mime="text/markdown")) is PreviewBucket.DOCUMENT


@pytest.mark.parametrize("name, descends", [("R&D", True), (".git", False), (".cache", False),
                                             ("node_modules", False), ("Site-Packages", False)])
def test_a_folder_is_descended_where_the_indexer_descends(name, descends):
    assert descend_into(_folder(name)) is descends


def test_a_file_is_never_descended_into():
    assert descend_into(_file("lease_porto.pdf")) is False


def test_a_repository_marker_among_the_children_makes_the_folder_opaque():
    assert is_marked_opaque([_file("lease_porto.pdf"), _folder(".git")]) is True
    assert is_marked_opaque([_file("pyvenv.cfg")]) is True
    assert is_marked_opaque([_file("lease_porto.pdf")]) is False


def test_large_turns_on_at_the_threshold_and_when_capped():
    below = ScopePreview(counts={PreviewBucket.DOCUMENT: PREVIEW_LARGE_FILES - 1})
    at = ScopePreview(counts={PreviewBucket.DOCUMENT: PREVIEW_LARGE_FILES - 5, PreviewBucket.IMAGE: 5})
    assert (below.large, at.large) == (False, True)
    assert at.total == PREVIEW_LARGE_FILES
    assert ScopePreview(counts={}, capped=True).large is True
