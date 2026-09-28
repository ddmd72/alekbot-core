"""CloudflareSfuAdapter.close(): failures are best-effort cleanup, not a service alarm — a real
close failure logs at WARNING exactly once (no ERROR, and no double log from `_call` plus
`close()`'s own except clause); an already-closed adapter (404/410) keeps its existing
INFO-level behaviour unchanged."""
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.adapters.cloudflare_sfu_adapter import CloudflareSfuAdapter

SECRET = "the-app-secret"


def _resp(status, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    r.text = json.dumps(body if body is not None else {})
    return r


def _adapter(client):
    return CloudflareSfuAdapter("app1", SECRET, http_client=client)


def _at_level(caplog, level):
    return [r for r in caplog.records if r.levelno == level]


@pytest.mark.asyncio
async def test_a_503_on_close_logs_exactly_one_warning_and_no_error(caplog):
    caplog.set_level(logging.INFO)
    client = AsyncMock()
    client.request.return_value = _resp(503, {"error": "unavailable"})

    await _adapter(client).close(["A-in"])

    assert _at_level(caplog, logging.ERROR) == []
    warnings = _at_level(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "A-in" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_transport_error_on_close_logs_exactly_one_warning_and_no_error(caplog):
    import httpx

    caplog.set_level(logging.INFO)
    client = AsyncMock()
    client.request.side_effect = httpx.ConnectError("no route")

    await _adapter(client).close(["A-in"])

    assert _at_level(caplog, logging.ERROR) == []
    warnings = _at_level(caplog, logging.WARNING)
    assert len(warnings) == 1
    assert "A-in" in warnings[0].getMessage()


@pytest.mark.asyncio
async def test_a_404_on_close_keeps_current_info_level_behaviour(caplog):
    caplog.set_level(logging.INFO)
    client = AsyncMock()
    client.request.return_value = _resp(404, {"errorCode": "not found"})

    await _adapter(client).close(["A-in"])

    assert _at_level(caplog, logging.ERROR) == []
    assert _at_level(caplog, logging.WARNING) == []
    assert any("already closed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_410_on_close_keeps_current_info_level_behaviour(caplog):
    caplog.set_level(logging.INFO)
    client = AsyncMock()
    client.request.return_value = _resp(410, {"errorCode": "gone"})

    await _adapter(client).close(["A-in"])

    assert _at_level(caplog, logging.ERROR) == []
    assert _at_level(caplog, logging.WARNING) == []
    assert any("already closed" in r.getMessage() for r in caplog.records)
