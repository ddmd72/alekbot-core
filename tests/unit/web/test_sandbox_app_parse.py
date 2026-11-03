"""The sandbox app with the real ParseEngine over a stub child (UNTRUSTED_CONTENT_SANDBOX_RFC §4.7; plan P1): every path
carries the outcome header, an ok answer's detail is the parser's own status, caps and bad plans are refused."""
import json
import logging
import os
import sys

import pytest
from quart import Quart

from src.adapters.sandbox_engine.child_launcher import ChildLauncher
from src.adapters.sandbox_engine.parse_engine import ParseEngine
from src.domain import sandbox_contract as sc
from src.domain.sandbox_contract import SandboxLimits, SandboxMeta, SandboxOp, SandboxStatus
from src.domain.sandbox_windows_frame import split_windows
from src.web import sandbox_app
from tests.unit.web.test_sandbox_app import CEILING, GOOD, FakeGate, FakeRecycler

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(os.geteuid() == 0 or sys.platform not in ("linux", "darwin"), reason="needs a non-root user"),
]
_STUB = os.path.join(os.path.dirname(__file__), "..", "adapters", "sandbox_engine", "parse_child_stub.py")
_OPS = (SandboxOp.PARSE_EXTRACT, SandboxOp.PARSE_PDF_PAGE_COUNT, SandboxOp.PARSE_PDF_WINDOWS)


def make_app(tmp_path):
    out_root = tmp_path / "out"
    out_root.mkdir(mode=0o700)
    work_root = tmp_path / "work"
    work_root.mkdir()
    launcher = ChildLauncher(nproc=4000, nofile=256)
    engines = {op: ParseEngine(launcher, op=op, output_root=str(out_root), child_script=os.path.abspath(_STUB))
               for op in _OPS}
    app = Quart(__name__)
    app.register_blueprint(sandbox_app.create_sandbox_blueprint(
        engines, FakeGate(), FakeRecycler(), CEILING,
        {"work_root": str(work_root), "engine_user": None, "recycle": "never", "output_root": str(out_root)}))
    return app, work_root, out_root


def headers(limits: SandboxLimits = CEILING, **meta):
    return {**GOOD, sc.META_HEADER: sc.encode_meta(SandboxMeta(request_id=sc.new_request_id(), limits=limits, **meta))}


def outcome(resp) -> sc.SandboxOutcome:
    return sc.decode_outcome(resp.headers[sc.OUTCOME_HEADER])


async def post(app, op, spec="ok", **meta):
    body = json.dumps({"*": spec}).encode() if isinstance(spec, str) else json.dumps(spec).encode()
    return await app.test_client().post(f"/v1/{op}", data=body, headers=headers(**meta))


async def test_extract_ok_header_detail_is_the_parser_status_and_the_body_is_the_result(tmp_path):
    app, work_root, out_root = make_app(tmp_path)
    resp = await post(app, "parse.extract", "status:encrypted", kind="pdf", ext="pdf")
    assert resp.status_code == 200
    out = outcome(resp)
    assert (out.status, out.detail) == (SandboxStatus.OK, "encrypted")
    assert json.loads(await resp.get_data())["status"] == "encrypted"
    assert list(work_root.iterdir()) == [] and list(out_root.iterdir()) == []


async def test_page_count_ok(tmp_path):
    app, *_ = make_app(tmp_path)
    resp = await post(app, "parse.pdf_page_count")
    assert (outcome(resp).status, outcome(resp).detail) == (SandboxStatus.OK, "text")
    assert json.loads(await resp.get_data()) == {"status": "text", "pages_total": 3}


async def test_windows_ok_streams_a_frame(tmp_path):
    app, *_ = make_app(tmp_path)
    resp = await post(app, "parse.pdf_windows", windows=((0, 2), (2, 2)))
    assert resp.status_code == 200 and outcome(resp).detail == "text"
    assert split_windows(await resp.get_data()) == [b"%PDF-window-0", b"%PDF-window-1"]


@pytest.mark.parametrize("spec, status, code", [
    ("sleep", SandboxStatus.TIMEOUT, 504),
    ("memory_error", SandboxStatus.MEMORY, 422),
    ("exit7", SandboxStatus.ERROR, 500),
    ("garbage", SandboxStatus.ERROR, 500),
])
async def test_every_failure_path_carries_the_outcome_header(tmp_path, spec, status, code):
    app, work_root, out_root = make_app(tmp_path)
    resp = await app.test_client().post(
        "/v1/parse.extract", data=json.dumps({"*": spec}).encode(),
        headers=headers(SandboxLimits(wall_s=1.5, memory_bytes=1 << 30, cpu_s=30, file_bytes=1 << 20), kind="txt"))
    assert resp.status_code == code
    out = outcome(resp)
    assert out.status is status and out.detail == ""   # a failure's reason is the text body, never the header
    assert list(work_root.iterdir()) == [] and list(out_root.iterdir()) == []


async def test_missing_kind_and_missing_windows_are_400(tmp_path):
    app, *_ = make_app(tmp_path)
    for op in ("parse.extract", "parse.pdf_windows"):
        resp = await post(app, op)
        assert resp.status_code == 400 and outcome(resp).status is SandboxStatus.BAD_REQUEST


async def test_a_bad_window_plan_in_the_header_is_400_and_starts_nothing(tmp_path):
    import base64

    app, work_root, _ = make_app(tmp_path)
    raw = json.dumps({"request_id": sc.new_request_id(), "windows": [[0, 0]],
                      "limits": {"wall_s": 1, "memory_bytes": 1, "cpu_s": 1, "file_bytes": 1}}).encode()
    bad = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
    resp = await app.test_client().post("/v1/parse.pdf_windows", data=b"{}", headers={**GOOD, sc.META_HEADER: bad})
    assert resp.status_code == 400 and outcome(resp).status is SandboxStatus.BAD_REQUEST
    assert list(work_root.iterdir()) == []


async def test_unknown_parse_op_is_404(tmp_path):
    app, *_ = make_app(tmp_path)
    resp = await post(app, "parse.pdf_window", windows=((0, 1),))
    assert resp.status_code == 404 and sc.OUTCOME_HEADER in resp.headers


async def test_413_by_content_length_and_by_streamed_bytes(tmp_path, monkeypatch):
    monkeypatch.setattr(sandbox_app, "REQUEST_CAP_BYTES", 250)
    app, work_root, _ = make_app(tmp_path)
    resp = await app.test_client().post("/v1/parse.pdf_page_count", data=b"y" * 300,
                                        headers={**headers(), "Content-Length": "300"})
    assert resp.status_code == 413 and outcome(resp).status is SandboxStatus.TOO_LARGE
    app.config["MAX_CONTENT_LENGTH"] = None
    async with app.test_client().request("/v1/parse.pdf_page_count", method="POST", headers=headers()) as conn:
        for _ in range(5):
            await conn.send(b"x" * 100)
        await conn.send_complete()
    resp = await conn.as_response()
    assert resp.status_code == 413 and outcome(resp).status is SandboxStatus.TOO_LARGE
    assert list(work_root.iterdir()) == []


async def test_non_ok_is_logged_at_warning_and_ok_at_info(tmp_path, caplog):
    app, *_ = make_app(tmp_path)
    with caplog.at_level(logging.INFO):
        await post(app, "parse.pdf_page_count")
        await post(app, "parse.pdf_page_count", "exit7")
    lines = [r for r in caplog.records if r.getMessage().startswith("[Sandbox] request_id=")]
    assert [(r.levelno, "status=ok" in r.getMessage()) for r in lines] == [(logging.INFO, True), (logging.WARNING, False)]
