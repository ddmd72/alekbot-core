"""U5 frozen core (A-U5-2): both parser adapters stamp `extracted_by` from the version the child reported, never from
their own constant: `parser@<reported>`, `parser@3` when an older child reports nothing, and an invalid result when
the report is malformed. The family is copied; an unknown one is unknown. The sandbox adapter is driven over a scripted
service (its wire), the in-process adapter through its result mapping."""
import json

import pytest

from src.adapters.sandbox_client.sandbox_content_extractor import SandboxContentExtractor
from src.adapters.sandbox_client.sandbox_http_client import SandboxHttpClient
from src.adapters.subprocess_content_extractor import SubprocessContentExtractor
from src.domain.document_extraction import ExtractionStatus
from src.domain.drive_extraction import ExtractionRequest
from src.domain.sandbox_contract import SandboxStatus
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import BASE, Scripted, good_token, reply

TEXT = {"status": "text", "text": "Rent is 950 EUR, Rua Fictícia 12, Lisboa.", "pages_total": 0, "truncated": False}


async def _no_sleep(_s: float) -> None:
    return None


async def _sandboxed(tmp_path, result: dict):
    body = json.dumps(result).encode()
    adapter = SandboxContentExtractor(SandboxHttpClient(BASE, good_token, transport=Scripted(
        reply(200, SandboxStatus.OK, body, detail=result["status"])), sleep=_no_sleep))
    path = tmp_path / "rent.eml"
    path.write_bytes(b"From: landlord@example.org\n\nRent is 950 EUR.")
    return await adapter.extract(ExtractionRequest(path=str(path), name="rent.eml", kind="eml", mime_type="",
                                                   user_id="u1", account_id="acc-1", item_id="i1"))


async def _in_process(_tmp_path, result: dict):
    return SubprocessContentExtractor()._from_result(result)


ADAPTERS = pytest.mark.parametrize("run", [_sandboxed, _in_process], ids=["sandbox", "in_process"])


@ADAPTERS
async def test_the_stamp_is_the_reported_version(run, tmp_path):
    out = await run(tmp_path, {**TEXT, "parser_version": 4, "reader_family": "eml"})
    assert out.status is ExtractionStatus.TEXT
    assert (out.extracted_by, out.reader_family) == ("parser@4", "eml")


@ADAPTERS
async def test_a_child_that_reports_nothing_is_parser_3(run, tmp_path):
    out = await run(tmp_path, TEXT)
    assert out.status is ExtractionStatus.TEXT
    assert (out.extracted_by, out.reader_family) == ("parser@3", "")


@ADAPTERS
async def test_a_failure_verdict_is_stamped_from_the_report_too(run, tmp_path):
    out = await run(tmp_path, {"status": "encrypted", "parser_version": 4, "reader_family": "pdf"})
    assert out.status is ExtractionStatus.ENCRYPTED and out.extracted_by == "parser@4" and out.reader_family == "pdf"


@ADAPTERS
async def test_an_unknown_family_is_unknown(run, tmp_path):
    out = await run(tmp_path, {**TEXT, "parser_version": 4, "reader_family": "spreadsheet9"})
    assert out.status is ExtractionStatus.TEXT and out.reader_family == ""


@ADAPTERS
@pytest.mark.parametrize("bad", [{"parser_version": "4"}, {"parser_version": 0}, {"reader_family": 3}])
async def test_a_malformed_report_is_an_invalid_result(run, bad, tmp_path):
    out = await run(tmp_path, {**TEXT, **bad})
    assert out.status is ExtractionStatus.ERROR and out.text == "" and out.detail == "invalid result"
