"""Index-run rules (USER_DRIVE_INDEXING_RFC §8.1, §8.2, §5.3, §5.4, §24.5, §24.8). Pure."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from src.domain import drive_index_policy as policy
from src.domain.drive_extraction import ExtractionStatus
from src.domain.drive_index import DriveChange, DriveIndexRecord, Priority, Reason, RecordState
from src.domain.drive_index_params import MAX_CHUNKS_PER_USER
from src.domain.drive_index_planner import content_due
from src.domain.drive_index_policy import SLICE2_CONTENT_KINDS, SLICE2_TRIAGED_KINDS
from src.domain.drive_index_run import (
    READER_IMAGE_MAX_BYTES, RunStep, camera_prior, chunk_cap_refuses, chunk_released, file_suffix, gate_exempt,
    image_mime, original_fallback, outcome_step, readme_choice, releasable, routed,
)

NOW = datetime(2026, 10, 12, 12, tzinfo=timezone.utc)


def rec(item_id="a", name="contract.pdf", kind="pdf", **kw):
    return DriveIndexRecord(user_id="u", connection_id="u", item_id=item_id, name=name, kind=kind, content_version="c1", **kw)


def refused(item_id, chunks, rank=0, days=0):
    return rec(item_id, state=RecordState.METADATA_ONLY, reason=Reason.USER_CHUNK_CAP, chunk_count=chunks,
               card_rank=rank, modified_at=NOW - timedelta(days=days))


class TestOutcomeStep:
    @pytest.mark.parametrize("status,expected", [
        (ExtractionStatus.TEXT, (RunStep.TEXT, None)),
        (ExtractionStatus.NO_TEXT, (RunStep.DEMOTE, Reason.NO_TEXT)),
        (ExtractionStatus.UNSUPPORTED, (RunStep.DEMOTE, Reason.KIND_NOT_ADMITTED)),
        (ExtractionStatus.TOO_LARGE, (RunStep.DEMOTE, Reason.TOO_LARGE)),
        (ExtractionStatus.ENCRYPTED, (RunStep.DEMOTE, Reason.ENCRYPTED)),
        (ExtractionStatus.TIMEOUT, (RunStep.RETRY, Reason.EXTRACT_TIMEOUT)),
        (ExtractionStatus.MEMORY, (RunStep.RETRY, Reason.EXTRACT_MEMORY)),
        (ExtractionStatus.ERROR, (RunStep.RETRY, Reason.EXTRACT_ERROR)),
        (ExtractionStatus.BUDGET_EXHAUSTED, (RunStep.WAIT_MONTH, None)),
    ])
    def test_mapping(self, status, expected):
        assert outcome_step(status) == expected

    def test_every_status_is_mapped(self):
        for status in ExtractionStatus:
            outcome_step(status)


class TestKinds:
    @pytest.mark.parametrize("kind", ["csv", "xlsx", "xls", "ods"])
    def test_tables_skip_the_gate(self, kind):
        assert gate_exempt(kind)

    @pytest.mark.parametrize("kind", ["text", "pdf", "markdown", "subtitles", "json"])
    def test_text_is_gated(self, kind):
        assert not gate_exempt(kind)

    def test_routed_kinds(self):
        assert routed("pdf") and routed("image") and not routed("docx") and not routed("text")


class TestCameraPrior:
    @pytest.mark.parametrize("facets,expected", [
        ({}, False),
        ({"photo": {}}, False),                                   # S0e: messenger JPEGs carry an empty photo
        ({"photo": {"takenDateTime": "2026-08-01T10:00:00Z"}}, True),
        ({"photo": {"cameraMake": "Apple"}}, True),
        ({"photo": {"cameraModel": "iPhone"}}, False),
        ({"photo": "odd"}, False),
        ({"image": {"width": 10}}, False),
    ])
    def test_prior(self, facets, expected):
        assert camera_prior(facets) is expected


class TestOriginalFallback:
    @pytest.mark.parametrize("name,mime,size,expected", [
        ("scan.jpg", "image/jpeg", 1000, True),
        ("shot.PNG", "", 1000, True),
        ("x.webp", "", READER_IMAGE_MAX_BYTES, True),
        ("anim.gif", "image/gif", 10, True),
        ("noext", "image/png", 10, True),
        ("IMG.HEIC", "image/heic", 1000, False),
        ("raw.dng", "", 1000, False),
        ("big.jpg", "image/jpeg", READER_IMAGE_MAX_BYTES + 1, False),
        ("empty.jpg", "image/jpeg", 0, False),
    ])
    def test_formats_and_cap(self, name, mime, size, expected):
        assert original_fallback(rec(name=name, kind="image", mime_type=mime, size_bytes=size)) is expected


class TestFileSuffix:
    @pytest.mark.parametrize("name,expected", [
        ("Договор.PDF", ".pdf"), ("notes.md", ".md"), ("x", ""), ("a.tar.gz", ".gz"),
        ("weird.p d f", ""), ("x.verylongext1", ""), ("../../etc.passwd", ".passwd"), ("x.ünï", ""),
    ])
    def test_suffix(self, name, expected):
        assert file_suffix(name) == expected


def test_image_mime():
    assert image_mime(b"\x89PNG\r\n\x1a\n") == "image/png"
    assert image_mime(b"\xff\xd8\xff\xe0") == "image/jpeg"


class TestChunkCap:
    def test_within_the_cap(self):
        assert not chunk_cap_refuses(MAX_CHUNKS_PER_USER - 10, rec(), 10)

    def test_past_the_cap(self):
        assert chunk_cap_refuses(MAX_CHUNKS_PER_USER - 10, rec(), 11)

    def test_the_replaced_generation_stops_counting(self):
        old = rec(indexed_generation=1, chunk_count=50)
        assert not chunk_cap_refuses(MAX_CHUNKS_PER_USER, old, 50)
        assert chunk_cap_refuses(MAX_CHUNKS_PER_USER, old, 51)

    def test_a_refused_count_is_not_readable(self):
        assert chunk_cap_refuses(MAX_CHUNKS_PER_USER, rec(chunk_count=50), 1)


class TestRelease:
    def test_in_order_while_headroom_lasts(self):
        recs = [refused("a", 30), refused("b", 30), refused("c", 30)]
        assert [r.item_id for r in releasable(recs, 70)] == ["a", "b"]

    def test_a_big_file_does_not_block_a_smaller_one(self):
        recs = [refused("big", 500), refused("small", 20)]
        assert [r.item_id for r in releasable(recs, 100)] == ["small"]

    def test_only_chunk_cap_refusals(self):
        other = replace(refused("x", 1), reason=Reason.NO_TEXT)
        assert releasable([other], 100) == []

    def test_no_headroom(self):
        assert releasable([refused("a", 1)], 0) == []

    def test_released_is_pending_in_class_3(self, monkeypatch):
        monkeypatch.setattr(policy, "ADMITTED_KINDS", SLICE2_CONTENT_KINDS)
        monkeypatch.setattr(policy, "TRIAGED_KINDS", SLICE2_TRIAGED_KINDS)
        out = chunk_released(refused("a", 30), NOW)
        assert (out.state, out.reason, out.chunk_count) == (RecordState.PENDING, None, 30)
        assert (out.due_at, out.priority) == (NOW, Priority.MAINTENANCE) and content_due(out, NOW)


class TestReadmeChoice:
    def change(self, name, item_id=None, **kw):
        return DriveChange(item_id=item_id or name, name=name, parent_id="r", **kw)

    def test_markdown_then_text_then_shortest_then_id(self):
        kids = [self.change("readme.txt"), self.change("README.rst"), self.change("README.markdown"),
                self.change("README.md", "b"), self.change("readme.md", "a"), self.change("main.py")]
        assert readme_choice(kids).item_id == "a"
        assert readme_choice([k for k in kids if not k.name.lower().endswith((".md", ".markdown"))]).name == \
            "readme.txt"

    def test_folders_deletions_and_other_names_are_not_readmes(self):
        kids = [self.change("README", is_folder=True), self.change("README.md", deleted=True),
                self.change("READ_ME.md")]
        assert readme_choice(kids) is None
