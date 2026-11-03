"""PdfSplitterPort on the sandboxed parser (EXTERNAL_FILES_RFC §4.4, §4.5; U1-8): `deadline_at` is epoch seconds and
only lowers the child's wall clock; a deadline already passed starts no child and fails `timeout` / `deadline`; a
window batch is all or nothing. Without `deadline_at` every path is today's (test_subprocess_content_extractor.py)."""
import asyncio
import os
import time

import pytest

from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import PdfSplitFailed
from src.ports.pdf_splitter_port import PdfSplitterPort
from tests.unit.adapters.drive_extraction_fixtures import make_pdf

STUB = "tests.unit.adapters.drive_parser_child_stub"


def _workroot(tmp_path):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


def _stub(tmp_path, **kwargs):
    return SubprocessContentExtractor(child_module=STUB, temp_root=str(_workroot(tmp_path)), **kwargs)


def _file(tmp_path, name, data=b"x"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


def _no_spawn(extractor):
    spawned = []

    async def spawn(*args, **kwargs):
        spawned.append(args)
        raise AssertionError("a child was started")

    extractor._spawn = spawn
    return spawned


def test_the_sandboxed_parser_is_the_splitter():
    assert isinstance(SubprocessContentExtractor(), PdfSplitterPort)


# =============================================================================
# deadline_at
# =============================================================================

async def test_an_expired_deadline_starts_no_child_and_fails_timeout_deadline(tmp_path):
    extractor = _stub(tmp_path)
    spawned = _no_spawn(extractor)
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.pdf_page_count(str(_file(tmp_path, "sleep")), deadline_at=time.time() - 1)
    assert failed.value.status is ExtractionStatus.TIMEOUT
    assert failed.value.detail == "deadline"
    assert spawned == []
    assert list(_workroot(tmp_path).iterdir()) == []


async def test_an_expired_deadline_cuts_no_window(tmp_path):
    extractor = _stub(tmp_path)
    spawned = _no_spawn(extractor)
    out = tmp_path / "w.pdf"
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.cut_pdf_window(str(_file(tmp_path, "sleep")), 0, 3, str(out), deadline_at=time.time())
    assert (failed.value.status, failed.value.detail) == (ExtractionStatus.TIMEOUT, "deadline")
    assert spawned == [] and not os.path.lexists(out)


async def test_an_expired_deadline_fails_a_window_batch_before_any_child(tmp_path):
    extractor = _stub(tmp_path)
    spawned = _no_spawn(extractor)
    outs = [str(tmp_path / "w0.pdf"), str(tmp_path / "w1.pdf")]
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.cut_pdf_windows(str(_file(tmp_path, "sleep")), [(0, 2), (2, 2)], outs,
                                        deadline_at=time.time() - 5)
    assert (failed.value.status, failed.value.detail) == (ExtractionStatus.TIMEOUT, "deadline")
    assert spawned == [] and not any(os.path.lexists(p) for p in outs)


@pytest.mark.slow
async def test_a_deadline_lowers_the_childs_wall_and_the_child_is_gone(tmp_path):
    extractor = _stub(tmp_path, wall_timeout_s=60)
    source = _file(tmp_path, "sleep")
    started = time.monotonic()
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.pdf_page_count(str(source), deadline_at=time.time() + 1.5)
    assert time.monotonic() - started < 30            # the stub sleeps 60 s; the 60 s wall would have waited it out
    assert (failed.value.status, failed.value.detail) == (ExtractionStatus.TIMEOUT, "deadline")
    pid = int((tmp_path / "sleep.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.slow
async def test_a_deadline_past_the_wall_never_raises_it(tmp_path):
    extractor = _stub(tmp_path, wall_timeout_s=1.5)
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.pdf_page_count(str(_file(tmp_path, "sleep")), deadline_at=time.time() + 120)
    assert (failed.value.status, failed.value.detail) == (ExtractionStatus.TIMEOUT, "wall clock 1.5s")


async def test_a_deadline_with_time_left_does_not_change_a_result(tmp_path):
    with pytest.raises(PdfSplitFailed) as failed:
        await _stub(tmp_path).pdf_page_count(str(_file(tmp_path, "split_encrypted")), deadline_at=time.time() + 60)
    assert failed.value.status is ExtractionStatus.ENCRYPTED


# =============================================================================
# cut_pdf_windows
# =============================================================================

@pytest.mark.slow
async def test_cut_pdf_windows_writes_every_window_in_order(tmp_path):
    source = make_pdf(tmp_path / "s.pdf", [f"page {i}" for i in range(1, 8)])
    outs = [str(tmp_path / f"window-{i}.pdf") for i in range(3)]
    extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)))
    cut = extractor.cut_pdf_window
    order = []

    async def recording(path, first_page, page_count, out_path, *, deadline_at=None):
        order.append((first_page, out_path))
        await cut(path, first_page, page_count, out_path, deadline_at=deadline_at)

    extractor.cut_pdf_window = recording
    await extractor.cut_pdf_windows(str(source), [(0, 3), (3, 3), (6, 3)], outs, deadline_at=time.time() + 120)
    assert order == [(0, outs[0]), (3, outs[1]), (6, outs[2])]
    assert [await extractor.pdf_page_count(p) for p in outs] == [3, 3, 1]


def _fake_cuts(extractor, fail_at, error):
    async def cut(path, first_page, page_count, out_path, *, deadline_at=None):
        with open(out_path, "wb") as fh:
            fh.write(b"%PDF-1.4 partial")
        if first_page == fail_at:
            raise error

    extractor.cut_pdf_window = cut


async def test_a_failed_window_leaves_no_window_file_of_the_batch(tmp_path):
    extractor = _stub(tmp_path)
    _fake_cuts(extractor, fail_at=4, error=PdfSplitFailed(ExtractionStatus.MEMORY, "killed"))
    outs = [str(tmp_path / f"w{i}.pdf") for i in range(3)]
    with pytest.raises(PdfSplitFailed) as failed:
        await extractor.cut_pdf_windows("source.pdf", [(0, 2), (2, 2), (4, 2)], outs)
    assert failed.value.status is ExtractionStatus.MEMORY
    assert not any(os.path.lexists(p) for p in outs)


async def test_a_cancelled_batch_leaves_no_window_file(tmp_path):
    extractor = _stub(tmp_path)
    _fake_cuts(extractor, fail_at=2, error=asyncio.CancelledError())
    outs = [str(tmp_path / f"w{i}.pdf") for i in range(3)]
    with pytest.raises(asyncio.CancelledError):
        await extractor.cut_pdf_windows("source.pdf", [(0, 2), (2, 2), (4, 2)], outs)
    assert not any(os.path.lexists(p) for p in outs)


async def test_windows_and_out_paths_must_pair_up(tmp_path):
    extractor = _stub(tmp_path)
    spawned = _no_spawn(extractor)
    with pytest.raises(ValueError):
        await extractor.cut_pdf_windows("source.pdf", [(0, 2), (2, 2)], [str(tmp_path / "w0.pdf")])
    assert spawned == []
