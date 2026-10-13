"""The content fakes behave like the ports they stand in for (drive_content_fakes.py); plan parts test against them."""
import pytest

from src.domain.drive_enrichment import PhotoDescription
from src.domain.drive_enrichment_calls import ModelReplyStatus
from src.domain.drive_extraction import (
    READER, DriveModelUnavailable, ExtractionOutcome, ExtractionRequest, ExtractionStatus, TriageImage,
)
from src.domain.drive_index import TriageVerdict
from src.domain.user_drive import DriveFileTooLarge, DriveItemNotFoundError

from .drive_content_fakes import FakeContentDrive, FakeExtractor, FakeTriage


def request(path, name="a.txt", kind="text"):
    return ExtractionRequest(path=str(path), name=name, kind=kind, mime_type="", user_id="u", account_id="a")


class TestFakeExtractor:
    async def test_reads_the_file_when_unscripted(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_text("hello", encoding="utf-8")
        ex = FakeExtractor()
        outcome = await ex.extract(request(f))
        assert (outcome.status, outcome.text, outcome.extracted_by) == (ExtractionStatus.TEXT, "hello", "parser@1")
        assert ex.requests == [request(f)]

    async def test_empty_file_has_no_text(self, tmp_path):
        f = tmp_path / "x.txt"
        f.write_bytes(b"")
        assert (await FakeExtractor().extract(request(f))).status is ExtractionStatus.NO_TEXT

    async def test_scripted_by_name_or_path_suffix(self, tmp_path):
        ex = FakeExtractor(READER, 2, {"scan.pdf": ExtractionOutcome(ExtractionStatus.TEXT, text="read"),
                                       ".bin": ExtractionOutcome(ExtractionStatus.TIMEOUT)})
        named = await ex.extract(request(tmp_path / "t1", name="scan.pdf"))
        assert (named.text, named.extracted_by) == ("read", "reader@2")
        assert (await ex.extract(request(tmp_path / "t2.bin", name="b"))).status is ExtractionStatus.TIMEOUT

    async def test_outage(self, tmp_path):
        ex = FakeExtractor(READER)
        ex.unavailable = True
        with pytest.raises(DriveModelUnavailable):
            await ex.extract(request(tmp_path / "x"))

    def test_identity(self):
        ex = FakeExtractor(READER, 3)
        assert (ex.extractor_id, ex.version) == (READER, 3)


class TestFakeTriage:
    """The fake answers as the triage v2 port does (USER_DRIVE_INDEXING_RFC §25.13.1): a TriageReply, an outage as a
    status."""

    async def test_scripted_and_default_verdicts(self):
        tr = FakeTriage({"a": (TriageVerdict.TEXT, "")}, cost_per_call=0.002)
        images = [TriageImage("a", b"1", "image/jpeg", False), TriageImage("b", b"2", "image/jpeg", True)]
        reply = await tr.triage("u", "acc", images)
        assert reply.status is ModelReplyStatus.OK
        assert [(r.item_id, r.verdict, r.photo) for r in reply.results] == [
            ("a", TriageVerdict.TEXT, None), ("b", TriageVerdict.PHOTO, PhotoDescription(description="a photo"))]
        assert (reply.model_calls, reply.usd, reply.fallback_usd) == (1, 0.002, 0.0)
        assert (reply.capped_item_ids, reply.refused_item_ids) == ((), ())
        assert tr.calls == [images]

    async def test_empty_batch_costs_nothing(self):
        reply = await FakeTriage(cost_per_call=0.002).triage("u", "acc", [])
        assert (reply.status, reply.results, reply.model_calls, reply.usd) == (ModelReplyStatus.OK, (), 0, 0.0)

    async def test_outage_is_a_status_not_an_exception(self):
        tr = FakeTriage(cost_per_call=0.002)
        tr.unavailable = True
        images = [TriageImage("a", b"1", "image/jpeg", False)]
        reply = await tr.triage("u", "a", images)
        assert (reply.status, reply.results, reply.model_calls, reply.usd) == (ModelReplyStatus.UNAVAILABLE, (), 0,
                                                                               0.0)
        assert tr.calls == [images]


class TestFakeContentDrive:
    async def test_download_to_file(self, tmp_path):
        drive = FakeContentDrive()
        item = drive.add_file("a.txt", b"hello")
        dest = tmp_path / "out"
        assert await drive.download_to_file("u", item, str(dest), 100) == 5
        assert dest.read_bytes() == b"hello" and drive.items[item].size_bytes == 5 and drive.downloads == [item]

    async def test_stream_cut_at_max_bytes(self, tmp_path):
        drive = FakeContentDrive()
        item = drive.add_file("big.pdf", b"x" * 10)
        dest = tmp_path / "out"
        with pytest.raises(DriveFileTooLarge):
            await drive.download_to_file("u", item, str(dest), 4)
        assert dest.read_bytes() == b"xxxx"

    async def test_missing_item_and_scripted_error(self, tmp_path):
        drive = FakeContentDrive()
        with pytest.raises(DriveItemNotFoundError):
            await drive.download_to_file("u", "nope", str(tmp_path / "o"), 10)
        item = drive.add_file("a.txt", b"x")
        drive.download_errors[item] = TimeoutError("slow")
        with pytest.raises(TimeoutError):
            await drive.download_to_file("u", item, str(tmp_path / "o"), 10)
        assert await drive.download_to_file("u", item, str(tmp_path / "o"), 10) == 1   # one-shot

    async def test_thumbnails(self):
        drive = FakeContentDrive()
        with_thumb = drive.add_file("a.jpg", b"img", thumbnail=b"thumb")
        without = drive.add_file("b.heic", b"img")
        assert await drive.get_thumbnail("u", with_thumb, "medium") == b"thumb"
        assert await drive.get_thumbnail("u", without, "medium") is None
        assert drive.thumbnail_calls == [(with_thumb, "medium"), (without, "medium")]

    async def test_edit_is_a_new_version_in_the_feed(self):
        drive = FakeContentDrive()
        item = drive.add_file("a.txt", b"one")
        before = len(drive.log)
        drive.edit(item, b"three")
        change = drive.items[item]
        assert change.content_version != "c1" and change.size_bytes == 5 and drive.log[before:] == [change]
