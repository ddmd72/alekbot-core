"""Lazy PDF windows for the reader (USER_DRIVE_INDEXING_RFC §24.3; adversarial review A "M split"): the sandboxed
parser counts a PDF's pages and cuts ONE window on demand, so the reader never holds more than MODEL_IN_FLIGHT
window files on disk. Readers in-process, the child end to end, and the adapter over the real and a stub child."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.adapters.drive_parser.readers import cut_pdf_window, pdf_page_count
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import ExtractionStatus, PdfSplitFailed
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_pdf

_ROOT = Path(__file__).resolve().parents[3]
STUB = "tests.unit.adapters.drive_parser_child_stub"
CUT_STUB = "tests.unit.adapters.drive_parser_cut_stub"


def _page_texts(path):
    from pypdf import PdfReader

    return [page.extract_text().strip() for page in PdfReader(str(path)).pages]


def _pdf(tmp_path, n=25):
    return make_pdf(tmp_path / "s.pdf", [f"page {i}" for i in range(1, n + 1)])


# =============================================================================
# readers (inside the child)
# =============================================================================

class TestReaders:
    def test_page_count(self, tmp_path):
        assert pdf_page_count(str(_pdf(tmp_path))) == 25

    def test_cut_one_window_writes_only_its_pages(self, tmp_path):
        out = tmp_path / "w.pdf"
        assert cut_pdf_window(str(_pdf(tmp_path)), 10, 10, str(out)) == 10
        assert _page_texts(out) == [f"page {i}" for i in range(11, 21)]
        assert sorted(p.name for p in tmp_path.iterdir()) == ["s.pdf", "w.pdf"]   # one window, nothing else

    def test_cut_past_the_end_is_clamped(self, tmp_path):
        out = tmp_path / "w.pdf"
        assert cut_pdf_window(str(_pdf(tmp_path)), 20, 10, str(out)) == 5
        assert _page_texts(out) == [f"page {i}" for i in range(21, 26)]

    @pytest.mark.parametrize("first,count", [(-1, 1), (0, 0), (25, 1)])
    def test_cut_outside_the_document_is_an_error(self, tmp_path, first, count):
        with pytest.raises(PdfSplitFailed) as failed:
            cut_pdf_window(str(_pdf(tmp_path)), first, count, str(tmp_path / "w.pdf"))
        assert failed.value.status is ExtractionStatus.ERROR

    def test_owner_password_only_cuts(self, tmp_path):
        source = encrypt_pdf(make_pdf(tmp_path / "a.pdf", ["a", "b"]), tmp_path / "r.pdf", user_password="")
        assert pdf_page_count(str(source)) == 2
        assert cut_pdf_window(str(source), 1, 1, str(tmp_path / "w.pdf")) == 1

    def test_user_password_is_encrypted(self, tmp_path):
        source = encrypt_pdf(make_pdf(tmp_path / "a.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
        for call in (lambda: pdf_page_count(str(source)),
                     lambda: cut_pdf_window(str(source), 0, 1, str(tmp_path / "w.pdf"))):
            with pytest.raises(PdfSplitFailed) as failed:
                call()
            assert failed.value.status is ExtractionStatus.ENCRYPTED

    def test_garbage_is_an_error(self, tmp_path):
        source = tmp_path / "g.pdf"
        source.write_bytes(b"garbage")
        with pytest.raises(PdfSplitFailed) as failed:
            pdf_page_count(str(source))
        assert failed.value.status is ExtractionStatus.ERROR

    def test_recursion_is_unsupported(self, tmp_path, monkeypatch):
        import pypdf

        def deep(*args, **kwargs):
            raise RecursionError("page tree loop")

        monkeypatch.setattr(pypdf, "PdfReader", deep)
        with pytest.raises(PdfSplitFailed) as failed:
            pdf_page_count(str(tmp_path / "x.pdf"))
        assert failed.value.status is ExtractionStatus.UNSUPPORTED

    def test_memory_failure_propagates_as_memory_error(self, tmp_path, monkeypatch):
        import pypdf

        def oom(*args, **kwargs):
            raise MemoryError()

        monkeypatch.setattr(pypdf, "PdfReader", oom)
        with pytest.raises(MemoryError):
            cut_pdf_window(str(tmp_path / "x.pdf"), 0, 1, str(tmp_path / "w.pdf"))


# =============================================================================
# the child, end to end
# =============================================================================

def _child(tmp_path, **fields):
    request = {"out": str(tmp_path / "result.json"), "memory_bytes": 2 * 1024 ** 3, "cpu_s": 30,
               "file_bytes": 64 * 1024 ** 2, **fields}
    proc = subprocess.run([sys.executable, "-B", "-E", "-s", "-m", "src.adapters.drive_parser.child"],
                          input=json.dumps(request).encode(), cwd=_ROOT, capture_output=True, timeout=60)
    assert proc.returncode == 0, proc.stderr
    return json.loads((tmp_path / "result.json").read_text())


class TestChildOps:
    def test_page_count_op(self, tmp_path):
        assert _child(tmp_path, op="pdf_page_count", path=str(_pdf(tmp_path, 3))) == \
            {"status": "text", "pages_total": 3}

    def test_cut_op(self, tmp_path):
        out = tmp_path / "w.pdf"
        result = _child(tmp_path, op="cut_pdf_window", path=str(_pdf(tmp_path, 3)), first_page=1, page_count=5,
                        out_path=str(out))
        assert result == {"status": "text", "pages": 2}
        assert _page_texts(out) == ["page 2", "page 3"]

    def test_cut_op_failure_is_a_status(self, tmp_path):
        source = encrypt_pdf(make_pdf(tmp_path / "a.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
        result = _child(tmp_path, op="cut_pdf_window", path=str(source), first_page=0, page_count=1,
                        out_path=str(tmp_path / "w.pdf"))
        assert result["status"] == "encrypted"


# =============================================================================
# the adapter
# =============================================================================

def _workroot(tmp_path):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


class TestAdapter:
    async def test_real_child_counts_and_cuts(self, tmp_path):
        extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)))
        source = _pdf(tmp_path, 7)
        assert await extractor.pdf_page_count(str(source)) == 7
        out = tmp_path / "w.pdf"
        await extractor.cut_pdf_window(str(source), 3, 3, str(out))
        assert _page_texts(out) == ["page 4", "page 5", "page 6"]
        assert list(_workroot(tmp_path).iterdir()) == []

    async def test_real_child_encrypted(self, tmp_path):
        locked = encrypt_pdf(make_pdf(tmp_path / "s.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
        extractor = SubprocessContentExtractor()
        with pytest.raises(PdfSplitFailed) as failed:
            await extractor.pdf_page_count(str(locked))
        assert failed.value.status is ExtractionStatus.ENCRYPTED
        with pytest.raises(PdfSplitFailed) as failed:
            await extractor.cut_pdf_window(str(locked), 0, 1, str(tmp_path / "w.pdf"))
        assert failed.value.status is ExtractionStatus.ENCRYPTED

    async def test_child_status_is_raised(self, tmp_path):
        source = tmp_path / "split_encrypted"
        source.write_bytes(b"x")
        stub = SubprocessContentExtractor(child_module=STUB, temp_root=str(_workroot(tmp_path)))
        with pytest.raises(PdfSplitFailed) as failed:
            await stub.cut_pdf_window(str(source), 0, 1, str(tmp_path / "w.pdf"))
        assert failed.value.status is ExtractionStatus.ENCRYPTED

    async def test_wall_clock_kill_is_timeout(self, tmp_path):
        source = tmp_path / "sleep"
        source.write_bytes(b"x")
        stub = SubprocessContentExtractor(child_module=STUB, temp_root=str(_workroot(tmp_path)), wall_timeout_s=1.5)
        with pytest.raises(PdfSplitFailed) as failed:
            await stub.pdf_page_count(str(source))
        assert failed.value.status is ExtractionStatus.TIMEOUT

    @pytest.mark.parametrize("behaviour", ["bad_count", "no_window", "symlink_window"])
    async def test_a_result_the_child_cannot_back_is_refused(self, tmp_path, behaviour):
        source = tmp_path / behaviour
        source.write_bytes(b"x")
        stub = SubprocessContentExtractor(child_module=CUT_STUB, temp_root=str(_workroot(tmp_path)))
        out = tmp_path / "w.pdf"
        with pytest.raises(PdfSplitFailed) as failed:
            if behaviour == "bad_count":
                await stub.pdf_page_count(str(source))
            else:
                await stub.cut_pdf_window(str(source), 0, 1, str(out))
        assert failed.value.status is ExtractionStatus.ERROR
        assert not os.path.lexists(out)   # a refused window is not left for the caller to read

    async def test_the_split_op_is_gone(self):
        assert not hasattr(SubprocessContentExtractor, "split_pdf")
