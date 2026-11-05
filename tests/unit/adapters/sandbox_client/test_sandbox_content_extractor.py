"""SandboxContentExtractor over a scripted service: the in-process adapter's mapping on every parser verdict, one
test per failure (outcome or exception plus a log record of at least WARNING), version parity, the closed wire meta,
the deadline and the all-or-nothing window batch."""
import base64
import json
import logging
import os
import time

import httpx
import pytest

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain import sandbox_contract as sc
from src.domain.document_extraction import PARSER, PARSER_VERSION, ExtractionStatus
from src.domain.drive_extraction import ExtractionRequest, ParserUnavailable, PdfSplitFailed
from src.domain.drive_index_params import MAX_TEXT_CHARS
from src.domain.sandbox_contract import SandboxStatus
from src.domain.sandbox_windows_frame import frame_windows
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, Scripted, good_token, reply

S = ExtractionStatus
EXTRACT = "parse.extract"


async def no_sleep(_s: float) -> None:
    return None


def extractor(*script):
    transport = Scripted(*script)
    return SandboxContentExtractor(SandboxHttpClient(BASE, good_token, transport=transport, sleep=no_sleep)), transport


def ok(result: dict, detail=None) -> httpx.Response:
    return reply(200, SandboxStatus.OK, json.dumps(result).encode(), detail=detail or result["status"])


def frame_reply(*windows: bytes) -> httpx.Response:
    return reply(200, SandboxStatus.OK, frame_windows(list(windows)), detail="text")


def meta_of(request: httpx.Request) -> sc.SandboxMeta:
    return sc.decode_meta(request.headers[sc.META_HEADER])


def make_file(tmp_path, name="lease_2026.docx", data=b"PK\x03\x04 rent"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def request_for(path, name="lease_2026.docx", kind="docx", **kw):
    return ExtractionRequest(path=path, name=name, kind=kind, mime_type="", user_id="ann", account_id="acc-1",
                             item_id="item-9", **kw)


def logged(caplog, level=logging.WARNING):
    return any(r.levelno >= level for r in caplog.records)


VERDICTS = [
    {"status": "text", "text": "Rent is due on the first.", "pages_total": 3, "truncated": False,
     "script_rendered": True, "capture_signals": {}},
    {"status": "text", "text": "   ", "pages_total": 1},
    {"status": "text", "text": "x" * (MAX_TEXT_CHARS + 5), "pages_total": 2},
    {"status": "no_text", "pages_total": 30, "detail": "scan", "capture_signals": {"hint": 1}},
    {"status": "unsupported", "detail": "format"},
    {"status": "too_large", "detail": "pages"},
    {"status": "encrypted"},
    {"status": "error", "detail": "d" * 400},
]


@pytest.mark.parametrize("result", VERDICTS, ids=lambda r: f"{r['status']}-{len(str(r))}")
async def test_every_parser_verdict_through_ok_equals_the_in_process_mapping(result, tmp_path):
    sandboxed, _ = extractor(ok(result))
    out = await sandboxed.extract(request_for(make_file(tmp_path)))
    assert out == SubprocessContentExtractor()._from_result(result)


async def test_versions_and_stamps_equal_the_in_process_adapter():
    sandboxed, _ = extractor(reply(200))
    local = SubprocessContentExtractor()
    assert (sandboxed.extractor_id, sandboxed.version, sandboxed.stamp) == (local.extractor_id, local.version, local.stamp)
    assert (sandboxed.extractor_id, sandboxed.version) == (PARSER, PARSER_VERSION)


async def test_the_wire_meta_is_the_closed_set_without_identity_and_ext_comes_from_the_name(tmp_path):
    sandboxed, transport = extractor(ok({"status": "text", "text": "a"}))
    path = make_file(tmp_path, name="input.bin")
    await sandboxed.extract(request_for(path, name="Lease_2026.DOCX"))
    request = transport.requests[0]
    meta = meta_of(request)
    assert (meta.kind, meta.ext, meta.windows) == ("docx", "docx", None)
    assert meta.limits.wall_s == sc.CEILING_WALL_S and meta.limits.file_bytes == sc.CEILING_FILE_BYTES
    wire = json.loads(base64.urlsafe_b64decode(request.headers[sc.META_HEADER] + "=="))
    assert set(wire) <= {"request_id", "limits", "kind", "ext", "windows"}
    blob = json.dumps(wire)
    for forbidden in ("ann", "acc-1", "item-9", "lease_2026"):
        assert forbidden not in blob
    assert request.url.path == f"/v1/{EXTRACT}" and request.content == b"PK\x03\x04 rent"


@pytest.mark.parametrize("name", ["no_extension", "archive.tar.gz!", "x." + "a" * 20])
async def test_an_unusable_extension_is_sent_as_none(name, tmp_path):
    sandboxed, transport = extractor(ok({"status": "text", "text": "a"}))
    await sandboxed.extract(request_for(make_file(tmp_path), name=name))
    assert meta_of(transport.requests[0]).ext is None


async def test_a_missing_file_and_a_file_over_its_cap_send_no_request(tmp_path, monkeypatch, caplog):
    sandboxed, transport = extractor(reply(200))
    with caplog.at_level(logging.WARNING):
        gone = await sandboxed.extract(request_for(str(tmp_path / "gone.docx")))
    assert gone.status is S.ERROR and gone.detail.startswith("stat:") and logged(caplog)
    path = make_file(tmp_path)
    monkeypatch.setattr("src.adapters.sandbox_client.sandbox_content_extractor.size_cap", lambda kind: 3)
    big = await sandboxed.extract(request_for(path))
    assert big.status is S.TOO_LARGE and transport.requests == []


async def test_a_file_over_the_request_cap_is_too_large_without_a_request(tmp_path, monkeypatch):
    monkeypatch.setattr("src.adapters.sandbox_client.sandbox_content_extractor.REQUEST_CAP_BYTES", 3)
    sandboxed, transport = extractor(reply(200))
    out = await sandboxed.extract(request_for(make_file(tmp_path)))
    assert out.status is S.TOO_LARGE and transport.requests == []


# --- failures: each ends as an outcome or an exception, with a log record -------------------------------------

@pytest.mark.parametrize("response,reason", [
    (reply(429), "busy"),
    (reply(503, SandboxStatus.RECYCLING), "busy"),
    (httpx.ConnectError("refused"), "unavailable"),
    (reply(401), "misconfigured"),
    (reply(403), "misconfigured"),
])
async def test_an_outage_is_parser_unavailable_never_the_files_outcome(response, reason, tmp_path, caplog):
    sandboxed, _ = extractor(response)
    with caplog.at_level(logging.INFO), pytest.raises(ParserUnavailable) as raised:
        await sandboxed.extract(request_for(make_file(tmp_path)))
    assert raised.value.reason == reason and raised.value.charges_file is False
    assert logged(caplog)


@pytest.mark.parametrize("response,status,detail", [
    (reply(504, SandboxStatus.TIMEOUT, b"", "wall clock 120s"), S.TIMEOUT, "wall clock 120s"),
    (reply(422, SandboxStatus.MEMORY, b"", "MemoryError"), S.MEMORY, "MemoryError"),
    (reply(500, SandboxStatus.ERROR, b"", "exit 7"), S.ERROR, "exit 7"),
    (httpx.ReadError("reset"), S.ERROR, "sandbox_lost"),
    (reply(413, SandboxStatus.TOO_LARGE, b"", "request_too_large"), S.TOO_LARGE, "request_too_large"),
])
async def test_a_run_failure_is_the_files_outcome_with_a_warning(response, status, detail, tmp_path, caplog):
    sandboxed, _ = extractor(response)
    with caplog.at_level(logging.INFO):
        out = await sandboxed.extract(request_for(make_file(tmp_path)))
    # no child result to report a version: the failure carries this client's own (current) parser version
    assert (out.status, out.detail) == (status, detail) and out.extracted_by == f"parser@{PARSER_VERSION}"
    assert logged(caplog)


async def test_an_engine_bad_request_is_an_error_outcome_with_an_error_line(tmp_path, caplog):
    sandboxed, _ = extractor(reply(400, SandboxStatus.BAD_REQUEST, b"", ""))
    with caplog.at_level(logging.INFO):
        out = await sandboxed.extract(request_for(make_file(tmp_path)))
    assert out.status is S.ERROR
    assert any(r.levelno == logging.ERROR and "bug" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("response", [
    reply(200, SandboxStatus.OK, b"not json", detail="text"),
    reply(200, SandboxStatus.OK, json.dumps({"status": "text", "brief": "x"}).encode(), detail="text"),
    reply(200, SandboxStatus.OK, json.dumps({"status": "text", "text": "a"}).encode(), detail="no_text"),
], ids=["not-json", "foreign-key", "header-body-mismatch"])
async def test_an_invalid_body_or_a_header_body_mismatch_is_an_invalid_result(response, tmp_path, caplog):
    sandboxed, _ = extractor(response)
    with caplog.at_level(logging.INFO):
        out = await sandboxed.extract(request_for(make_file(tmp_path)))
    assert (out.status, out.detail) == (S.ERROR, "invalid result") and logged(caplog)


# --- the deadline ---------------------------------------------------------------------------------------------

async def test_the_wall_is_lowered_to_the_time_left_and_its_timeout_is_a_deadline(tmp_path):
    sandboxed, transport = extractor(reply(504, SandboxStatus.TIMEOUT, b"", "wall clock 5s"))
    out = await sandboxed.extract(request_for(make_file(tmp_path), deadline_at=time.time() + 5))
    assert 0 < meta_of(transport.requests[0]).limits.wall_s <= 5
    assert (out.status, out.detail) == (S.TIMEOUT, "deadline")


async def test_a_deadline_further_than_the_ceiling_leaves_the_ceiling(tmp_path):
    sandboxed, transport = extractor(ok({"status": "text", "text": "a"}))
    await sandboxed.extract(request_for(make_file(tmp_path), deadline_at=time.time() + 10_000))
    assert meta_of(transport.requests[0]).limits.wall_s == sc.CEILING_WALL_S


async def test_a_passed_deadline_sends_no_request(tmp_path, caplog):
    sandboxed, transport = extractor(reply(200))
    with caplog.at_level(logging.WARNING):
        out = await sandboxed.extract(request_for(make_file(tmp_path), deadline_at=time.time() - 1))
    assert (out.status, out.detail) == (S.TIMEOUT, "deadline") and transport.requests == [] and logged(caplog)


# --- the page ops ---------------------------------------------------------------------------------------------

async def test_the_page_count_is_read_from_the_body(tmp_path):
    sandboxed, transport = extractor(ok({"status": "text", "pages_total": 42}))
    assert await sandboxed.pdf_page_count(make_file(tmp_path, "scan_0012.pdf", b"%PDF-")) == 42
    assert transport.requests[0].url.path == "/v1/parse.pdf_page_count"
    assert meta_of(transport.requests[0]).ext == "pdf" and meta_of(transport.requests[0]).kind is None


@pytest.mark.parametrize("status", ["encrypted", "unsupported", "error", "too_large"])
async def test_a_non_text_page_count_verdict_is_a_split_failure_with_that_status(status, tmp_path):
    sandboxed, _ = extractor(ok({"status": status, "detail": "why"}))
    with pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"))
    assert (raised.value.status, raised.value.detail) == (ExtractionStatus(status), "why")


@pytest.mark.parametrize("response,status", [
    (reply(504, SandboxStatus.TIMEOUT, b"", "wall"), S.TIMEOUT),
    (reply(422, SandboxStatus.MEMORY, b"", "m"), S.MEMORY),
    (reply(500, SandboxStatus.ERROR, b"", "e"), S.ERROR),
])
async def test_a_page_op_run_failure_is_a_split_failure(response, status, tmp_path, caplog):
    sandboxed, _ = extractor(response)
    with caplog.at_level(logging.INFO), pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"))
    assert raised.value.status is status and logged(caplog)


async def test_a_page_op_outage_is_parser_unavailable(tmp_path):
    sandboxed, _ = extractor(reply(429))
    with pytest.raises(ParserUnavailable) as raised:
        await sandboxed.pdf_page_count(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"))
    assert raised.value.reason == "busy"


async def test_a_page_op_passed_deadline_and_an_oversized_pdf_send_no_request(tmp_path, monkeypatch):
    sandboxed, transport = extractor(reply(200))
    path = make_file(tmp_path, "scan_0012.pdf", b"%PDF-")
    with pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(path, deadline_at=time.time() - 1)
    assert (raised.value.status, raised.value.detail) == (S.TIMEOUT, "deadline")
    monkeypatch.setattr("src.adapters.sandbox_client.sandbox_content_extractor.size_cap", lambda kind: 2)
    with pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(path)
    assert raised.value.status is S.TOO_LARGE and transport.requests == []


async def test_cut_pdf_window_is_a_batch_of_one(tmp_path):
    sandboxed, transport = extractor(frame_reply(b"%PDF-window"))
    out = tmp_path / "w.pdf"
    await sandboxed.cut_pdf_window(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"), 10, 5, str(out))
    assert out.read_bytes() == b"%PDF-window"
    assert meta_of(transport.requests[0]).windows == ((10, 5),)
    assert transport.requests[0].url.path == "/v1/parse.pdf_windows"


async def test_cut_pdf_windows_is_one_request_with_every_window(tmp_path):
    sandboxed, transport = extractor(frame_reply(b"A", b"BB", b"CCC"))
    outs = [str(tmp_path / f"w{i}.pdf") for i in range(3)]
    await sandboxed.cut_pdf_windows(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"),
                                    [(0, 10), (10, 10), (20, 5)], outs)
    assert [open(o, "rb").read() for o in outs] == [b"A", b"BB", b"CCC"]
    assert len(transport.requests) == 1 and meta_of(transport.requests[0]).windows == ((0, 10), (10, 10), (20, 5))


@pytest.mark.parametrize("response", [
    frame_reply(b"A"),                                                    # one window for two asked
    reply(200, SandboxStatus.OK, b"\x00\x00\x00\x02\x00\x00", detail="text"),   # a short frame
    reply(200, SandboxStatus.OK, frame_windows([b"A", b"B"]) + b"junk", detail="text"),   # trailing bytes
    ok({"status": "encrypted", "detail": "locked"}),                      # a verdict for the whole batch
    reply(504, SandboxStatus.TIMEOUT, b"", "wall"),
    reply(429),
], ids=["count", "short-frame", "trailing", "verdict", "timeout", "outage"])
async def test_windows_are_all_or_nothing(response, tmp_path, caplog):
    sandboxed, _ = extractor(response)
    outs = [str(tmp_path / "w0.pdf"), str(tmp_path / "w1.pdf")]
    with caplog.at_level(logging.INFO), pytest.raises((PdfSplitFailed, ParserUnavailable)):
        await sandboxed.cut_pdf_windows(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"), [(0, 5), (5, 5)], outs)
    assert not any(os.path.exists(o) for o in outs) and logged(caplog)


async def test_a_write_failure_removes_the_windows_already_written(tmp_path):
    sandboxed, _ = extractor(frame_reply(b"A", b"B"))
    first = str(tmp_path / "w0.pdf")
    second = str(tmp_path / "missing_dir" / "w1.pdf")
    with pytest.raises(OSError):
        await sandboxed.cut_pdf_windows(make_file(tmp_path, "scan_0012.pdf", b"%PDF-"), [(0, 5), (5, 5)],
                                        [first, second])
    assert not os.path.exists(first)


async def test_window_argument_errors(tmp_path):
    sandboxed, transport = extractor(reply(200))
    path = make_file(tmp_path, "scan_0012.pdf", b"%PDF-")
    with pytest.raises(ValueError):
        await sandboxed.cut_pdf_windows(path, [(0, 1)], [])
    await sandboxed.cut_pdf_windows(path, [], [])
    with pytest.raises(PdfSplitFailed):
        await sandboxed.cut_pdf_windows(path, [(i, 1) for i in range(sc.SANDBOX_MAX_WINDOWS + 1)],
                                        [str(tmp_path / f"{i}.pdf") for i in range(sc.SANDBOX_MAX_WINDOWS + 1)])
    assert transport.requests == []
