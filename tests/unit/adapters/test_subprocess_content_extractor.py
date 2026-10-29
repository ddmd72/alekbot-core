"""SubprocessContentExtractor: one sandboxed child per file (USER_DRIVE_INDEXING_RFC §9, §24.3, §8.2 outcomes).
Real-child tests prove the protocol end to end; the stub child (drive_parser_child_stub) misbehaves on purpose
to prove every kill and every bad result maps to an outcome."""
import asyncio
import json
import os
import sys

import pytest

from src.adapters import subprocess_content_extractor as module
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.drive_extraction import PARSER, ExtractionRequest, ExtractionStatus, PdfSplitFailed
from src.domain.drive_index_params import MAX_DOWNLOAD_BYTES, MAX_PDF_DOWNLOAD_BYTES, MAX_TEXT_CHARS
from src.ports.content_extractor_port import ContentExtractorPort
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_docx, make_member_bomb, make_pdf

STUB = "tests.unit.adapters.drive_parser_child_stub"
CUT_STUB = "tests.unit.adapters.drive_parser_cut_stub"


def _request(path, kind):
    return ExtractionRequest(path=str(path), name=os.path.basename(str(path)), kind=kind, mime_type="",
                             user_id="u1", account_id="a1")


def _stub(tmp_path, **kwargs):
    return SubprocessContentExtractor(child_module=STUB, temp_root=str(_workroot(tmp_path)), **kwargs)


def _workroot(tmp_path):
    root = tmp_path / "work"
    root.mkdir(exist_ok=True)
    return root


def _file(tmp_path, name="input.bin", data=b"x"):
    path = tmp_path / name
    path.write_bytes(data)
    return path


# =============================================================================
# Identity
# =============================================================================

def test_exit_codes_mirror_the_child():
    from src.adapters.drive_parser import child

    assert (module.EXIT_MEMORY, module.EXIT_BAD_REQUEST) == (child.EXIT_MEMORY, child.EXIT_BAD_REQUEST)
    assert module.CHILD_MODULE == child.__name__


def test_is_the_parser_link():
    extractor = SubprocessContentExtractor()
    assert isinstance(extractor, ContentExtractorPort)
    assert extractor.extractor_id == PARSER
    assert isinstance(extractor.version, int) and extractor.version >= 1
    assert extractor.stamp == f"{PARSER}@{extractor.version}"


# =============================================================================
# The real child, end to end
# =============================================================================

async def test_real_child_reads_text_and_stamps_extracted_by(tmp_path):
    path = _file(tmp_path, "note.txt", "Орендна плата 12 500,00 грн".encode("cp1251") * 20)
    extractor = SubprocessContentExtractor(temp_root=str(_workroot(tmp_path)))
    outcome = await extractor.extract(_request(path, "text"))
    assert outcome.status is ExtractionStatus.TEXT
    assert outcome.text.startswith("Орендна плата 12 500,00 грн")
    assert outcome.extracted_by == f"{PARSER}@{SubprocessContentExtractor.VERSION}"
    assert list(_workroot(tmp_path).iterdir()) == []          # the call's private directory is gone


@pytest.mark.slow
async def test_real_child_reads_a_docx(tmp_path):
    path = make_docx(tmp_path / "a.docx", ["Penalty is 0.1% per day."])
    outcome = await SubprocessContentExtractor().extract(_request(path, "docx"))
    assert "Penalty is 0.1% per day." in outcome.text


@pytest.mark.slow
async def test_real_child_pdf_pages_and_encryption(tmp_path):
    plain = make_pdf(tmp_path / "a.pdf", ["first", "second", "third"])
    extractor = SubprocessContentExtractor()
    outcome = await extractor.extract(_request(plain, "pdf"))
    assert outcome.status is ExtractionStatus.TEXT and outcome.pages_total == 3
    locked = encrypt_pdf(plain, tmp_path / "locked.pdf", user_password="pw")
    encrypted = await extractor.extract(_request(locked, "pdf"))
    assert encrypted.status is ExtractionStatus.ENCRYPTED
    assert encrypted.extracted_by == extractor.stamp


async def test_real_child_refuses_the_phase0_member_bomb(tmp_path):
    outcome = await SubprocessContentExtractor().extract(_request(make_member_bomb(tmp_path / "b.xlsx"), "xlsx"))
    assert outcome.status is ExtractionStatus.TOO_LARGE


async def test_real_child_no_text(tmp_path):
    outcome = await SubprocessContentExtractor().extract(_request(make_pdf(tmp_path / "s.pdf", ["", ""]), "pdf"))
    assert outcome.status is ExtractionStatus.NO_TEXT and outcome.pages_total == 2 and outcome.text == ""


@pytest.mark.slow
async def test_real_child_splits_a_pdf(tmp_path):
    source = make_pdf(tmp_path / "s.pdf", [f"page {i}" for i in range(1, 8)])
    out = tmp_path / "windows"
    out.mkdir()
    extractor = SubprocessContentExtractor()
    assert await extractor.pdf_page_count(str(source)) == 7
    for first, name in ((0, "window-0000.pdf"), (3, "window-0001.pdf")):
        await extractor.cut_pdf_window(str(source), first, 3, str(out / name))
    assert sorted(os.listdir(out)) == ["window-0000.pdf", "window-0001.pdf"]
    assert all(os.path.isfile(out / name) for name in os.listdir(out))


async def test_real_child_split_of_an_encrypted_pdf_raises_encrypted(tmp_path):
    locked = encrypt_pdf(make_pdf(tmp_path / "s.pdf", ["a"]), tmp_path / "l.pdf", user_password="pw")
    with pytest.raises(PdfSplitFailed) as failed:
        await SubprocessContentExtractor().pdf_page_count(str(locked))
    assert failed.value.status is ExtractionStatus.ENCRYPTED
    with pytest.raises(PdfSplitFailed) as failed:
        await SubprocessContentExtractor().cut_pdf_window(str(locked), 0, 10, str(tmp_path / "w.pdf"))
    assert failed.value.status is ExtractionStatus.ENCRYPTED


# =============================================================================
# Checks before any child starts
# =============================================================================

async def test_file_over_the_sniffed_kinds_cap_is_too_large_without_a_child(tmp_path):
    path = tmp_path / "big.xlsx"
    with open(path, "wb") as fh:
        fh.truncate(11 * 1024 * 1024)                       # sparse: over the 10 MiB sheet cap
    extractor = SubprocessContentExtractor(child_module="no.such.module")
    outcome = await extractor.extract(_request(path, "xlsx"))
    assert outcome.status is ExtractionStatus.TOO_LARGE


def _sparse(tmp_path, name, size):
    path = tmp_path / name
    with open(path, "wb") as fh:
        fh.truncate(size)
    return path


async def test_a_pdf_between_the_chat_cap_and_the_pdf_cap_reaches_the_child(tmp_path):
    size = (MAX_DOWNLOAD_BYTES + MAX_PDF_DOWNLOAD_BYTES) // 2
    outcome = await _stub(tmp_path).extract(_request(_sparse(tmp_path, "large.pdf", size), "pdf"))
    # the stub child answered (it has no behaviour for "pdf"): the size check let the file through
    assert (outcome.status, outcome.detail) == (ExtractionStatus.ERROR, "stub has no behaviour 'pdf'")


async def test_a_download_sniffed_as_another_kind_keeps_that_kinds_cap(tmp_path):
    # a .pdf name downloaded under the PDF cap whose bytes are a docx: refused before any child
    size = (MAX_DOWNLOAD_BYTES + MAX_PDF_DOWNLOAD_BYTES) // 2
    extractor = SubprocessContentExtractor(child_module="no.such.module")
    outcome = await extractor.extract(_request(_sparse(tmp_path, "large.pdf", size), "docx"))
    assert outcome.status is ExtractionStatus.TOO_LARGE


async def test_missing_file_is_an_error_outcome(tmp_path):
    outcome = await SubprocessContentExtractor().extract(_request(tmp_path / "gone.txt", "text"))
    assert outcome.status is ExtractionStatus.ERROR


# =============================================================================
# Kills and limits map to outcomes (§8.2: timeout and memory are per-file transient)
# =============================================================================

@pytest.mark.slow
async def test_wall_clock_kill_is_timeout_and_the_child_is_gone(tmp_path):
    path = _file(tmp_path)
    outcome = await _stub(tmp_path, wall_timeout_s=1.5).extract(_request(path, "sleep"))
    assert outcome.status is ExtractionStatus.TIMEOUT
    pid = int((tmp_path / "input.bin.pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


@pytest.mark.slow
async def test_cpu_limit_is_timeout(tmp_path):
    outcome = await _stub(tmp_path, cpu_limit_s=1, wall_timeout_s=30).extract(_request(_file(tmp_path), "spin"))
    assert outcome.status is ExtractionStatus.TIMEOUT
    assert "cpu" in outcome.detail


async def test_memory_error_in_the_child_is_memory(tmp_path):
    outcome = await _stub(tmp_path).extract(_request(_file(tmp_path), "memory_error"))
    assert outcome.status is ExtractionStatus.MEMORY


async def test_sigkill_from_the_kernel_is_memory(tmp_path):
    outcome = await _stub(tmp_path).extract(_request(_file(tmp_path), "sigkill"))
    assert outcome.status is ExtractionStatus.MEMORY


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="RLIMIT_AS is enforced on Linux only (S1, gen2)")
async def test_address_space_limit_is_memory_on_linux(tmp_path):
    extractor = _stub(tmp_path, memory_limit_bytes=256 * 1024 * 1024, wall_timeout_s=30)
    outcome = await extractor.extract(_request(_file(tmp_path), "allocate"))
    assert outcome.status is ExtractionStatus.MEMORY


async def test_a_lazy_import_refused_by_the_address_space_limit_is_memory(tmp_path):
    outcome = await _stub(tmp_path).extract(_request(_file(tmp_path), "import_enomem"))
    assert outcome.status is ExtractionStatus.MEMORY


@pytest.mark.parametrize("kind", ["exit7", "no_result", "garbage", "bad_status", "bad_shape"])
async def test_a_child_that_fails_or_lies_is_an_error(tmp_path, kind):
    outcome = await _stub(tmp_path).extract(_request(_file(tmp_path), kind))
    assert outcome.status is ExtractionStatus.ERROR
    assert outcome.text == ""


async def test_limits_the_child_cannot_apply_are_an_error(tmp_path):
    extractor = _stub(tmp_path, cpu_limit_s=-5)               # setrlimit refuses it inside the child
    outcome = await extractor.extract(_request(_file(tmp_path), "huge_text"))
    assert outcome.status is ExtractionStatus.ERROR


async def test_text_over_the_cap_from_the_child_is_cut_and_truncated(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "MAX_TEXT_CHARS", 1000)
    extractor = _stub(tmp_path)
    original = extractor._run

    async def with_chars(payload):
        return await original({**payload, "chars": 5000})

    monkeypatch.setattr(extractor, "_run", with_chars)
    outcome = await extractor.extract(_request(_file(tmp_path), "huge_text"))
    assert outcome.status is ExtractionStatus.TEXT
    assert len(outcome.text) == 1000 and outcome.truncated is True


def test_result_size_bound_covers_the_text_cap():
    assert module._RESULT_MAX_BYTES >= MAX_TEXT_CHARS * 6


# =============================================================================
# Environment, temp directory, concurrency, cancellation
# =============================================================================

async def test_child_environment_is_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "secret-value")
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/creds.json")
    monkeypatch.setenv("PYTHONPATH", "/elsewhere")
    outcome = await _stub(tmp_path).extract(_request(_file(tmp_path), "env"))
    env = json.loads(outcome.text)
    assert "GEMINI_API_KEY" not in env and "GOOGLE_APPLICATION_CREDENTIALS" not in env
    assert "PYTHONPATH" not in env
    assert env["OMP_NUM_THREADS"] == "1" and env["MALLOC_ARENA_MAX"] == "2"
    assert env["TMPDIR"].startswith(str(_workroot(tmp_path)))
    assert list(_workroot(tmp_path).iterdir()) == []


@pytest.mark.slow
async def test_one_child_at_a_time_by_default(tmp_path):
    marks = _file(tmp_path, "marks", b"")
    extractor = _stub(tmp_path)
    await asyncio.gather(*(extractor.extract(_request(marks, "mark")) for _ in range(3)))
    events = [line.split() for line in marks.read_text().splitlines()]
    assert [kind for kind, _ in events] == ["start", "end"] * 3


async def test_cancelled_extraction_kills_the_child(tmp_path):
    path = _file(tmp_path)
    task = asyncio.create_task(_stub(tmp_path, wall_timeout_s=60).extract(_request(path, "sleep")))
    pid_file = tmp_path / "input.bin.pid"
    for _ in range(100):
        if pid_file.exists() and pid_file.read_text():
            break
        await asyncio.sleep(0.05)
    pid = int(pid_file.read_text())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    assert list(_workroot(tmp_path).iterdir()) == []


# =============================================================================
# page-window op failures (pdf_page_count / cut_pdf_window, §24.3)
# =============================================================================

async def test_split_result_naming_files_outside_its_directory_is_refused(tmp_path):
    # the lazy cut names no paths; a child that answers with a window linked to a file outside it is refused
    out = tmp_path / "w.pdf"
    stub = SubprocessContentExtractor(child_module=CUT_STUB, temp_root=str(_workroot(tmp_path)))
    with pytest.raises(PdfSplitFailed) as failed:
        await stub.cut_pdf_window(str(_file(tmp_path, "symlink_window")), 0, 10, str(out))
    assert failed.value.status is ExtractionStatus.ERROR
    assert not os.path.lexists(out)


async def test_split_status_from_the_child_is_raised(tmp_path):
    source = str(_file(tmp_path, "split_encrypted"))
    with pytest.raises(PdfSplitFailed) as failed:
        await _stub(tmp_path).pdf_page_count(source)
    assert failed.value.status is ExtractionStatus.ENCRYPTED
    with pytest.raises(PdfSplitFailed) as failed:
        await _stub(tmp_path).cut_pdf_window(source, 0, 10, str(tmp_path / "w.pdf"))
    assert failed.value.status is ExtractionStatus.ENCRYPTED


@pytest.mark.slow
async def test_split_killed_by_the_wall_clock_is_timeout(tmp_path):
    with pytest.raises(PdfSplitFailed) as failed:
        await _stub(tmp_path, wall_timeout_s=1.5).pdf_page_count(str(_file(tmp_path, "sleep")))
    assert failed.value.status is ExtractionStatus.TIMEOUT
