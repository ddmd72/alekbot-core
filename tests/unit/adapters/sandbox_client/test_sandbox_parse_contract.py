"""Wire contract of the sandboxed parser: the real sandbox app with the real light registry (the three `parse.*` ops over
the real parser child) behind `SandboxContentExtractor` over ASGI, against `SubprocessContentExtractor` on the same
files: equal outcomes per reachable status, equal page counts, equal windows (ADAPTER_WIRE_TESTING.md)."""
import os
import sys

import httpx
import pytest
from pypdf import PdfReader
from quart import Quart

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.parse_engine import ParseEngine
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionRequest, PdfSplitFailed
from src.domain.sandbox_contract import SandboxOp
from src.web import sandbox_app
from tests.unit.adapters.drive_extraction_fixtures import encrypt_pdf, make_docx, make_pdf
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, good_token
from tests.unit.web.test_sandbox_app import CEILING, FakeGate, FakeRecycler

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]
_OPS = (SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS)
S = ExtractionStatus



def _parser_over_the_real_app(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    work_root = tmp_path / "work"
    work_root.mkdir()
    launcher = ChildLauncher(nproc=4000, nofile=256)
    engines = {op: ParseEngine(launcher, op=op, output_root=str(out_root)) for op in _OPS}
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        engines, FakeGate(), FakeRecycler(), CEILING,
        {"work_root": str(work_root), "engine_user": None, "recycle": "never", "output_root": str(out_root)}))
    return SandboxContentExtractor(SandboxHttpClient(BASE, good_token, transport=httpx.ASGITransport(app=app)))


def _request(path, kind):
    return ExtractionRequest(path=str(path), name=os.path.basename(str(path)), kind=kind, mime_type="",
                             user_id="u1", account_id="a1")


def _same(a, b):
    assert (a.status, a.text, a.pages_total, a.truncated, a.detail, a.script_rendered, a.capture_signals,
            a.extracted_by) == (b.status, b.text, b.pages_total, b.truncated, b.detail, b.script_rendered,
                                b.capture_signals, b.extracted_by)


@pytest.fixture
def pair(tmp_path):
    return _parser_over_the_real_app(tmp_path), SubprocessContentExtractor()


async def test_documents_extract_to_the_same_outcome_as_in_process(pair, tmp_path):
    sandboxed, local = pair
    pdf = make_pdf(tmp_path / "lease_2026.pdf", ["Rent is due on the first.", "Notice period is two months."])
    scan = make_pdf(tmp_path / "scan_0012.pdf", ["", ""])
    locked = encrypt_pdf(pdf, tmp_path / "locked.pdf", user_password="secret")
    docx = make_docx(tmp_path / "lease_2026.docx", ["Rent is due on the first."])
    junk = tmp_path / "notes.bin"
    junk.write_bytes(b"\x00\x01\x02 not a document")
    seen = set()
    for path, kind in [(pdf, "pdf"), (scan, "pdf"), (locked, "pdf"), (docx, "docx"), (junk, "pdf")]:
        remote, here = await sandboxed.extract(_request(path, kind)), await local.extract(_request(path, kind))
        _same(remote, here)
        seen.add(remote.status)
    assert {S.TEXT, S.NO_TEXT, S.ENCRYPTED} <= seen


async def test_page_count_and_windows_equal_the_in_process_ones(pair, tmp_path):
    sandboxed, local = pair
    pdf = make_pdf(tmp_path / "lease_2026.pdf", [f"Page {i} of the lease." for i in range(1, 8)])
    assert await sandboxed.pdf_page_count(str(pdf)) == await local.pdf_page_count(str(pdf)) == 7
    plan = [(0, 3), (3, 3), (6, 5)]
    remote_out = [str(tmp_path / f"r{i}.pdf") for i in range(3)]
    local_out = [str(tmp_path / f"l{i}.pdf") for i in range(3)]
    await sandboxed.cut_pdf_windows(str(pdf), plan, remote_out)
    await local.cut_pdf_windows(str(pdf), plan, local_out)
    for remote, here in zip(remote_out, local_out):
        pages = [p.extract_text() for p in PdfReader(remote).pages]
        assert pages == [p.extract_text() for p in PdfReader(here).pages]
    assert [len(PdfReader(p).pages) for p in remote_out] == [3, 3, 1]


async def test_an_encrypted_pdf_fails_its_page_ops_like_in_process(pair, tmp_path):
    sandboxed, local = pair
    pdf = make_pdf(tmp_path / "lease_2026.pdf", ["Rent is due on the first."])
    locked = encrypt_pdf(pdf, tmp_path / "locked.pdf", user_password="secret")
    caught = []
    for extractor in (sandboxed, local):
        with pytest.raises(PdfSplitFailed) as raised:
            await extractor.pdf_page_count(str(locked))
        caught.append((raised.value.status, raised.value.detail))
    assert caught[0] == caught[1] and caught[0][0] is S.ENCRYPTED
