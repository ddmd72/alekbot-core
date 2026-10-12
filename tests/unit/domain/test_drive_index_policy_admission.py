"""Content admission (USER_DRIVE_INDEXING_RFC §5.1, §5.3, §24.1): empty until the flip, read at call time."""
import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_card import _KIND_LABEL
from src.domain.drive_extraction import chain_for
from src.domain.drive_index import ContentClass
from src.domain.drive_index_params import MAX_DOWNLOAD_BYTES, MAX_SHEET_DOWNLOAD_BYTES
from src.domain.drive_index_policy import (
    SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS, TRIAGED_KINDS, Admission, admission, content_class_of, size_cap,
)

ALL_KINDS = sorted(set(policy._EXT_KIND.values()) | {"other", "folder"})


class TestSliceOneIsUnchanged:
    def test_nothing_is_triaged_yet(self):
        assert TRIAGED_KINDS == frozenset()

    @pytest.mark.parametrize("kind", ALL_KINDS)
    def test_every_kind_is_metadata_only(self, kind):
        assert admission(kind) is Admission.NONE


class TestAdmission:
    def test_reads_the_sets_at_call_time(self, monkeypatch):
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset({"pdf"}))
        monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset({"image"}))
        assert admission("pdf") is Admission.CONTENT
        assert admission("image") is Admission.TRIAGE
        assert admission("docx") is Admission.NONE

    def test_content_wins_over_triage(self, monkeypatch):
        monkeypatch.setattr(policy, "ADMITTED_KINDS", frozenset({"image"}))
        monkeypatch.setattr(policy, "TRIAGED_KINDS", frozenset({"image"}))
        assert admission("image") is Admission.CONTENT


class TestFlipValues:
    def test_content_kinds_are_the_named_extractor_kinds(self):
        assert SLICE2_CONTENT_KINDS == {"pdf", "docx", "pptx", "xlsx", "xls", "odt", "ods", "odp", "epub", "html",
                                        "markdown", "text", "csv", "json", "rtf", "msg", "eml", "subtitles"}

    @pytest.mark.parametrize("kind", ["doc", "ppt", "mhtml", "iwork"])
    def test_kinds_without_an_extractor_stay_out(self, kind):
        assert kind not in SLICE2_CONTENT_KINDS and kind not in SLICE2_TRIAGED_KINDS

    def test_every_image_is_triaged(self):
        assert SLICE2_TRIAGED_KINDS == {"image"}

    @pytest.mark.parametrize("kind", sorted(SLICE2_CONTENT_KINDS))
    def test_content_kinds_are_documents_with_a_card_label_and_a_parser(self, kind):
        assert content_class_of(kind) is ContentClass.DOCUMENT
        assert kind in _KIND_LABEL
        assert chain_for(kind)[0] == "parser"


class TestSizeCap:
    @pytest.mark.parametrize("kind", ["xlsx", "xls"])
    def test_sheets(self, kind):
        assert size_cap(kind) == MAX_SHEET_DOWNLOAD_BYTES

    @pytest.mark.parametrize("kind", ["pdf", "docx", "ods", "csv", "image"])
    def test_everything_else(self, kind):
        assert size_cap(kind) == MAX_DOWNLOAD_BYTES
