"""DriveReaderRunner with lazy PDF windows (USER_DRIVE_INDEXING_RFC §24.3; adversarial review A "M split"): the
page count first, then one window cut on demand per call, so at most MODEL_IN_FLIGHT window files exist on disk at a
time — proved on a PDF whose 200 pages share one large image (every eager window carried the whole image)."""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace
from typing import List, Tuple
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.composition.drive_reader_runner import DriveReaderRunner
from src.domain.drive_extraction import ExtractionRequest, ExtractionStatus, PdfSplitFailed
from src.domain.drive_index_params import MODEL_EXTRACT_MAX_PAGES, MODEL_IN_FLIGHT, MODEL_PAGES_PER_CALL
from src.domain.llm import FinishReason
from src.domain.user import UserBotConfig
from tests.unit.agents.drive_model_fakes import RoutedLLM, context, file_bytes, prompt_builder, reply

S = ExtractionStatus


class FakeCutter:
    """Counts `pages_total` pages; a cut writes `<first>-<last>` (1-based pages) to the caller's path. Records the
    window files on disk right after every cut."""

    def __init__(self, pages_total: int, count_error: Exception = None, cut_error_at: int = -1,
                 cut_error: Exception = None) -> None:
        self.pages_total = pages_total
        self.count_error = count_error
        self.cut_error_at = cut_error_at
        self.cut_error = cut_error
        self.counts: List[str] = []
        self.cuts: List[Tuple[int, int]] = []
        self.on_disk: List[int] = []

    async def pdf_page_count(self, path):
        self.counts.append(path)
        if self.count_error is not None:
            raise self.count_error
        return self.pages_total

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        self.cuts.append((first_page, page_count))
        if first_page == self.cut_error_at:
            await asyncio.sleep(0.05)   # the windows before it answer first
            raise self.cut_error
        await asyncio.sleep(0)
        last = min(first_page + page_count, self.pages_total)
        Path(out_path).write_bytes(f"{first_page + 1}-{last}".encode())
        self.on_disk.append(len(os.listdir(os.path.dirname(out_path))))


def _key(request) -> str:
    return file_bytes(request).decode()


def _text(request):
    return reply({"text": f"text of {_key(request)}", "brief": None})


def _runner(handler, cutter, tmp_path, budget=600.0):
    llm = RoutedLLM(handler)
    users = AsyncMock()
    users.get_user.return_value = SimpleNamespace(config=UserBotConfig())
    contexts = MagicMock()
    contexts.build.return_value = context(llm)
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    runner = DriveReaderRunner(contexts, users, prompt_builder(), cutter, file_budget_s=budget,
                               temp_root=str(work))
    return runner, llm, work


def _request(tmp_path, pages=500, source=None):
    path = source or tmp_path / "file.pdf"
    if source is None:
        path.write_bytes(b"SOURCE")
    return ExtractionRequest(path=str(path), name="scan.pdf", kind="pdf", mime_type="application/pdf",
                             user_id="user-1", account_id="acc-1", max_model_pages=pages)


async def test_windows_are_cut_on_demand_and_joined_in_page_order(tmp_path):
    async def handler(request):
        await asyncio.sleep(0.01)
        return _text(request)

    cutter = FakeCutter(95)
    runner, llm, work = _runner(handler, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.TEXT and out.pages_total == 95 and out.pages_sent == 95 and out.model_calls == 10
    assert out.text.split("\n\n") == [f"text of {i * 10 + 1}-{min(i * 10 + 10, 95)}" for i in range(10)]
    assert sorted(cutter.cuts) == [(i * 10, MODEL_PAGES_PER_CALL) for i in range(9)] + [(90, 5)]
    assert max(cutter.on_disk) <= MODEL_IN_FLIGHT and llm.peak == MODEL_IN_FLIGHT
    assert list(work.iterdir()) == []   # every window file and the file's directory are gone


async def test_the_month_cap_limits_the_windows_cut(tmp_path):
    cutter = FakeCutter(25)
    runner, _, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path, pages=12))
    assert sorted(cutter.cuts) == [(0, 10), (10, 2)]
    assert (out.pages_total, out.pages_sent, out.truncated) == (25, 12, True)


async def test_max_pages_caps_a_long_file(tmp_path):
    cutter = FakeCutter(MODEL_EXTRACT_MAX_PAGES + 50)
    runner, _, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path, pages=10_000))
    assert sum(c for _, c in cutter.cuts) == MODEL_EXTRACT_MAX_PAGES and out.truncated is True


@pytest.mark.parametrize("status", [S.ENCRYPTED, S.TIMEOUT, S.MEMORY, S.UNSUPPORTED, S.ERROR])
async def test_a_page_count_failure_is_the_outcome(tmp_path, status):
    cutter = FakeCutter(25, count_error=PdfSplitFailed(status, "child said so"))
    runner, llm, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path))
    assert out.status is status and "child said so" in out.detail and llm.requests == [] and cutter.cuts == []


async def test_no_pages_is_no_text(tmp_path):
    runner, llm, _ = _runner(_text, FakeCutter(0), tmp_path)
    assert (await runner.extract(_request(tmp_path))).status is S.NO_TEXT and llm.requests == []


async def test_a_cut_failure_mid_file_is_the_outcome_and_keeps_the_usage(tmp_path):
    cutter = FakeCutter(25, cut_error_at=20, cut_error=PdfSplitFailed(S.MEMORY, "killed"))
    runner, _, _ = _runner(_text, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path))
    assert out.status is S.MEMORY and "killed" in out.detail
    assert out.pages_sent == 20 and out.model_calls == 2 and out.model_cost > 0


async def test_an_output_limited_window_is_halved_from_the_source(tmp_path):
    def handler(request):
        if _key(request) == "11-20":
            return reply(text="{", finish=FinishReason.MAX_TOKENS)
        return _text(request)

    cutter = FakeCutter(25)
    runner, _, work = _runner(handler, cutter, tmp_path)
    out = await runner.extract(_request(tmp_path))
    assert (10, 5) in cutter.cuts and (15, 5) in cutter.cuts
    assert out.text == "text of 1-10\n\ntext of 11-15\n\ntext of 16-20\n\ntext of 21-25"
    assert max(cutter.on_disk) <= MODEL_IN_FLIGHT and list(work.iterdir()) == []


# =============================================================================
# The disk bound on a real PDF through the real sandboxed parser
# =============================================================================

def _shared_image_pdf(path: Path, pages: int, image_bytes: int) -> Path:
    """`pages` pages that all draw one large image object: small on disk, but every window carries the image."""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, StreamObject

    writer = PdfWriter()
    image = StreamObject()
    image._data = os.urandom(image_bytes)
    image.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                  NameObject("/Width"): NumberObject(512), NameObject("/Height"): NumberObject(512),
                  NameObject("/ColorSpace"): NameObject("/DeviceGray"),
                  NameObject("/BitsPerComponent"): NumberObject(8)})
    image_ref = writer._add_object(image)
    for _ in range(pages):
        page = writer.add_blank_page(612, 792)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/XObject"): DictionaryObject({NameObject("/Im0"): image_ref})})
        content = DecodedStreamObject()
        content.set_data(b"q 100 0 0 100 0 0 cm /Im0 Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
    with open(path, "wb") as fh:
        writer.write(fh)
    return path


class _MeasuredParser(SubprocessContentExtractor):
    """The real parser; after every cut it records the bytes of all window files on disk."""

    def __init__(self, work: Path) -> None:
        super().__init__()
        self.work = work
        self.totals: List[int] = []
        self.windows: List[int] = []

    async def cut_pdf_window(self, path, first_page, page_count, out_path):
        await super().cut_pdf_window(path, first_page, page_count, out_path)
        self.windows.append(os.path.getsize(out_path))
        self.totals.append(sum(f.stat().st_size for f in self.work.rglob("*") if f.is_file()))


@pytest.mark.slow
async def test_window_bytes_on_disk_never_exceed_model_in_flight_windows(tmp_path):
    source = _shared_image_pdf(tmp_path / "shared.pdf", 200, 1024 * 1024)
    assert source.stat().st_size < 2 * 1024 * 1024   # one image, 200 pages

    async def handler(request):
        await asyncio.sleep(0.05)
        return reply({"text": "a picture", "brief": None})

    work = tmp_path / "work"
    work.mkdir()
    parser = _MeasuredParser(work)
    runner, _, _ = _runner(handler, parser, tmp_path)
    # 60 of the 200 pages this month: six windows, more than MODEL_IN_FLIGHT, at a third of the child processes
    out = await runner.extract(_request(tmp_path, pages=60, source=source))
    windows = 60 // MODEL_PAGES_PER_CALL
    assert out.pages_total == 200 and out.pages_sent == 60 and len(parser.windows) == windows > MODEL_IN_FLIGHT
    assert min(parser.windows) > 1024 * 1024                     # each window carries the whole image
    assert max(parser.totals) <= MODEL_IN_FLIGHT * max(parser.windows)
    assert max(parser.totals) < windows * min(parser.windows)    # what cutting every window at once held
    assert list(work.iterdir()) == []
