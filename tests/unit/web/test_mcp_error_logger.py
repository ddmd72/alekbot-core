"""McpErrorLogger — makes the SDK's reason for a /mcp 4xx visible (prod log audit C-12)."""
import logging

import pytest

from src.web.mcp_error_logger import McpErrorLogger


def _scope(path="/mcp", headers=None, type_="http"):
    return {
        "type": type_, "path": path, "method": "POST",
        "headers": [(k.encode(), v.encode()) for k, v in (headers or {}).items()],
    }


def _app(status, body=b'{"error":"Bad Request: Unsupported protocol version: 2099-01-01"}', chunks=1):
    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": status, "headers": []})
        step = max(1, len(body) // chunks)
        parts = [body[i:i + step] for i in range(0, len(body), step)] or [b""]
        for i, part in enumerate(parts):
            await send({"type": "http.response.body", "body": part, "more_body": i < len(parts) - 1})
    return app


async def _call(app, scope):
    sent = []

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request"}

    await McpErrorLogger(app)(scope, receive, send)
    return sent


async def test_400_logs_the_reason_and_the_headers_that_explain_it(caplog):
    scope = _scope(headers={"mcp-protocol-version": "2099-01-01", "content-type": "application/json",
                            "authorization": "Bearer SECRET-TOKEN"})
    with caplog.at_level(logging.INFO):
        sent = await _call(_app(400), scope)

    assert sent[0]["status"] == 400  # the response passes through untouched
    text = caplog.text
    assert "mcp_request_rejected status=400" in text
    assert "Unsupported protocol version" in text
    assert "protocol_version=2099-01-01" in text
    assert "has_session_id=False" in text
    assert "SECRET-TOKEN" not in text


async def test_session_id_is_reported_as_present_never_its_value(caplog):
    with caplog.at_level(logging.INFO):
        await _call(_app(400), _scope(headers={"mcp-session-id": "abc-123"}))
    assert "has_session_id=True" in caplog.text
    assert "abc-123" not in caplog.text


@pytest.mark.parametrize("status", [200, 202, 401])
async def test_successful_and_unauthenticated_responses_are_not_logged(caplog, status):
    with caplog.at_level(logging.INFO):
        await _call(_app(status), _scope())
    assert "mcp_request_rejected" not in caplog.text


async def test_other_paths_and_non_http_scopes_pass_through_silently(caplog):
    with caplog.at_level(logging.INFO):
        await _call(_app(400), _scope(path="/token"))
        await _call(_app(400), _scope(type_="lifespan"))
    assert "mcp_request_rejected" not in caplog.text


async def test_chunked_body_is_joined_and_capped(caplog):
    body = b"x" * 5000
    with caplog.at_level(logging.INFO):
        await _call(_app(400, body=body, chunks=10), _scope())
    logged = [r.getMessage() for r in caplog.records if "mcp_request_rejected" in r.getMessage()]
    assert len(logged) == 1
    assert logged[0].endswith("body=" + "x" * 512)
