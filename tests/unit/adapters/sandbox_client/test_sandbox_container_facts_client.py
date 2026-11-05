"""`SandboxContentExtractor.container_facts` (`ContainerFactsPort`, EXTERNAL_FILES D13 / owner decision O2) over a
scripted service: the closed request (raw bytes, no kind, no ext, no identity), the facts of an `ok` answer, every run
failure and invalid answer as `ERROR` facts (never an exception), and an outage as `ParserUnavailable`."""
import json
import logging
import time

import httpx
import pytest

from src.domain import sandbox_contract as sc
from src.domain.container_facts import ContainerFacts, ContainerStatus
from src.domain.drive_extraction import ParserUnavailable
from src.domain.sandbox_contract import SandboxStatus
from tests.unit.adapters.sandbox_client.sandbox_client_helpers import reply
from tests.unit.adapters.sandbox_client.test_sandbox_content_extractor import extractor, meta_of

ZIP = {"status": "zip", "members": 3, "vba_project": True, "macro_content_type": True, "odf_macros": False}
BODY = b"PK\x03\x04 bathroom quote"


def _ok(result: dict, detail=None) -> httpx.Response:
    return reply(200, SandboxStatus.OK, json.dumps(result).encode(), detail=detail or result["status"])


async def test_the_request_is_the_bytes_and_the_limits_only():
    sandboxed, transport = extractor(_ok(ZIP))
    facts = await sandboxed.container_facts(BODY)
    assert facts == ContainerFacts(status=ContainerStatus.ZIP, members=3, vba_project=True, macro_content_type=True)
    (request,) = transport.requests
    assert request.url.path == "/v1/parse.container_facts" and request.content == BODY
    meta = meta_of(request)
    assert (meta.kind, meta.ext, meta.windows) == (None, None, None)


@pytest.mark.parametrize("result", [{"status": "not_zip"}, {"status": "too_large", "members": 10_001, "detail": "cap"},
                                    {"status": "error", "detail": "BadZipFile"}])
async def test_the_op_s_own_verdicts_come_back_as_facts(result):
    sandboxed, _ = extractor(_ok(result))
    facts = await sandboxed.container_facts(BODY)
    assert facts.status is ContainerStatus(result["status"]) and not facts.carries_macros


@pytest.mark.parametrize("response", [
    reply(504, SandboxStatus.TIMEOUT, b"", detail="wall clock 120s"),
    reply(500, SandboxStatus.MEMORY, b"", detail="killed"),
    reply(413, SandboxStatus.TOO_LARGE, b"", detail="request too large"),
])
async def test_a_run_failure_is_unchecked_facts_not_an_exception(response, caplog):
    sandboxed, _ = extractor(response)
    with caplog.at_level(logging.INFO):
        facts = await sandboxed.container_facts(BODY)
    assert facts.status is ContainerStatus.ERROR and not facts.checked
    assert any(r.levelno >= logging.WARNING for r in caplog.records)


@pytest.mark.parametrize("response", [
    _ok({**ZIP, "user_id": "u1"}),                 # outside the closed schema
    _ok(ZIP, detail="not_zip"),                    # the body contradicts its header
    reply(200, SandboxStatus.OK, b"not json", detail="zip"),
])
async def test_an_invalid_answer_is_unchecked(response):
    sandboxed, _ = extractor(response)
    facts = await sandboxed.container_facts(BODY)
    assert (facts.status, facts.detail) == (ContainerStatus.ERROR, "invalid result")


async def test_an_outage_is_parser_unavailable():
    sandboxed, _ = extractor(reply(429, SandboxStatus.ERROR))
    with pytest.raises(ParserUnavailable) as raised:
        await sandboxed.container_facts(BODY)
    assert raised.value.reason == "busy"


async def test_a_passed_deadline_sends_nothing():
    sandboxed, transport = extractor(_ok(ZIP))
    facts = await sandboxed.container_facts(BODY, deadline_at=time.time() - 1)
    assert (facts.status, facts.detail) == (ContainerStatus.ERROR, "deadline") and transport.requests == []


async def test_a_body_above_the_request_cap_sends_nothing(monkeypatch):
    from src.adapters.sandbox_client import sandbox_content_extractor as module

    monkeypatch.setattr(module, "REQUEST_CAP_BYTES", 8)
    sandboxed, transport = extractor(_ok(ZIP))
    facts = await sandboxed.container_facts(BODY)
    assert facts.status is ContainerStatus.TOO_LARGE and transport.requests == []


async def test_the_deadline_lowers_the_wall():
    sandboxed, transport = extractor(_ok(ZIP))
    await sandboxed.container_facts(BODY, deadline_at=time.time() + 5)
    assert meta_of(transport.requests[0]).limits.wall_s <= 5
    assert meta_of(transport.requests[0]).limits.memory_bytes == sc.CEILING_MEMORY_BYTES
