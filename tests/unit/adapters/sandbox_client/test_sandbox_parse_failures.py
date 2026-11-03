"""The parse client's remaining failure paths: an unreadable PDF, a malformed page count, a body that contradicts its
header, a result with a bad field, and a recycling envelope that arrives with a 200."""
import logging

import pytest

from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import PdfSplitFailed
from src.domain.sandbox_contract import SandboxOp, SandboxStatus
from src.domain.sandbox_errors import SandboxUnavailable
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, LIMITS, Scripted, good_token, reply
from tests.unit.adapters.sandbox_client.test_sandbox_content_extractor import extractor, no_sleep, ok


async def test_a_pdf_that_cannot_be_stat_ed_is_an_error_with_a_warning(tmp_path, caplog):
    sandboxed, transport = extractor(ok({"status": "text", "pages_total": 1}))
    with caplog.at_level(logging.INFO), pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(str(tmp_path / "absent.pdf"))
    assert (raised.value.status, raised.value.detail) == (ExtractionStatus.ERROR, "stat: FileNotFoundError")
    assert transport.requests == []
    assert any(r.levelno == logging.WARNING and "cannot stat" in r.getMessage() for r in caplog.records)


async def test_a_page_count_of_the_wrong_shape_is_an_error(tmp_path, caplog, monkeypatch):
    sandboxed, _ = extractor(ok({"status": "text", "pages_total": 1}))

    async def bad_op(*_a, **_k):
        return {"status": "text", "pages_total": True}

    monkeypatch.setattr(sandboxed, "_page_op", bad_op)
    with caplog.at_level(logging.INFO), pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(str(tmp_path / "x.pdf"))
    assert (raised.value.status, raised.value.detail) == (ExtractionStatus.ERROR, "invalid page count")
    assert any(r.levelno == logging.WARNING and "invalid shape" in r.getMessage() for r in caplog.records)


async def test_a_page_count_body_that_contradicts_its_header_is_an_invalid_result(tmp_path):
    pdf = tmp_path / "x.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    sandboxed, _ = extractor(ok({"status": "text", "pages_total": 3}, detail="no_text"))
    with pytest.raises(PdfSplitFailed) as raised:
        await sandboxed.pdf_page_count(str(pdf))
    assert (raised.value.status, raised.value.detail) == (ExtractionStatus.ERROR, "invalid result")


def test_a_result_with_a_bad_field_is_an_error_outcome_with_a_warning(caplog):
    sandboxed, _ = extractor(ok({"status": "text", "pages_total": 1}))
    with caplog.at_level(logging.INFO):
        out = sandboxed._from_result({"status": "text", "text": 5, "pages_total": 1})
    assert (out.status, out.detail) == (ExtractionStatus.ERROR, "invalid result")
    assert any(r.levelno == logging.WARNING and "invalid shape" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("status,reason", [(SandboxStatus.RECYCLING, "busy"), (SandboxStatus.UNCONFIGURED, "unavailable")])
async def test_a_recycling_or_unconfigured_envelope_on_a_200_is_unavailable_without_retry(status, reason):
    transport = Scripted(reply(200, status))
    client = SandboxHttpClient(BASE, good_token, transport=transport, sleep=no_sleep)
    with pytest.raises(SandboxUnavailable) as raised:
        await client.call(SandboxOp.PARSE_EXTRACT, b"b", LIMITS)
    assert raised.value.reason == reason and len(transport.requests) == 1
