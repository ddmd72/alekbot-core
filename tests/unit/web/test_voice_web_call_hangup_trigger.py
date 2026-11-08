"""/hangup logs its trigger from a closed set; the page sends it and no longer hangs up on hide."""
import logging
import pathlib

import pytest

from tests.unit.web.test_voice_web_call_app import AUTH, MemoryStore, _app, _room, _start

CALL_HTML = pathlib.Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html"


async def _hangup(caplog, **kwargs):
    caplog.set_level(logging.INFO)
    client = _app(MemoryStore(), _room()).test_client()
    _, body = await _start(client)
    resp = await client.post(f"/api/voice/web-call/{body['call_id']}/hangup", headers=AUTH, **kwargs)
    assert resp.status_code == 200
    return [r.getMessage() for r in caplog.records if "hangup trigger=" in r.getMessage()]


@pytest.mark.asyncio
@pytest.mark.parametrize("trigger", ["button", "status_ended", "start_failed", "superseded"])
async def test_known_trigger_is_logged(caplog, trigger):
    lines = await _hangup(caplog, json={"trigger": trigger})
    assert len(lines) == 1 and lines[0].endswith(f"hangup trigger={trigger}")


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [{"trigger": "<script>"}, {"trigger": 5}, {}, [1]])
async def test_unknown_trigger_is_logged_as_unknown(caplog, payload):
    lines = await _hangup(caplog, json=payload)
    assert lines[0].endswith("hangup trigger=unknown")


@pytest.mark.asyncio
async def test_missing_body_is_unknown(caplog):
    lines = await _hangup(caplog)
    assert lines[0].endswith("hangup trigger=unknown")


def test_pagehide_sends_no_hangup():
    html = CALL_HTML.read_text()
    start = html.index('addEventListener("pagehide"')
    handler = html[start:html.index("});", start)]
    assert "fetch(" not in handler
    assert "stopRingback()" in handler


def test_every_page_hangup_call_names_a_trigger():
    html = CALL_HTML.read_text()
    assert html.count("/hangup`") == 2
    for trigger in ("status_ended", "start_failed", "superseded"):
        assert f'"{trigger}"' in html
