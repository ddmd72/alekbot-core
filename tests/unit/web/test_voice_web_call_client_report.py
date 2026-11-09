"""Playback report route: closed stages and keys, one sorted log line, ownership, size cap; and the page's side
(three stages, keepalive, no `.play(` on the remote audio, the page's keys equal the server allowlist)."""
import json
import logging
import re
from pathlib import Path

import pytest

from src.web.voice_web_call_app import _CLIENT_REPORT_KEYS, _CLIENT_REPORT_STAGES
from tests.unit.web.test_voice_web_call_app import AUTH, MemoryStore, _app, _room, _start

HTML = (Path(__file__).resolve().parents[3] / "src" / "web" / "static" / "call.html").read_text(encoding="utf-8")


def _report_lines(caplog):
    return [r.getMessage() for r in caplog.records if ": client " in r.getMessage()]


async def _started():
    client = _app(MemoryStore(), _room()).test_client()
    _, body = await _start(client)
    return client, body["call_id"]


@pytest.mark.asyncio
async def test_owned_call_logs_one_sorted_line_with_rounding_truncation_and_ua(caplog):
    client, call_id = await _started()
    caplog.set_level(logging.INFO)
    data = {"pc_state": "c" * 60, "el_volume": 0.123456, "el_paused": True, "rx_packets": 7}
    resp = await client.post(f"/api/voice/web-call/{call_id}/client-report", headers={**AUTH, "User-Agent": "u" * 200},
                             json={"stage": "live_3s", "data": data})
    assert resp.status_code == 200 and await resp.get_json() == {"ok": True}
    assert _report_lines(caplog) == [
        f"web call {call_id}: client live_3s el_paused=True el_volume=0.123 pc_state={'c' * 40} rx_packets=7 "
        f"ua={'u' * 120}"]


@pytest.mark.asyncio
async def test_unknown_key_and_structured_values_are_dropped(caplog):
    client, call_id = await _started()
    caplog.set_level(logging.INFO)
    data = {"secret": "x", "tr_muted": {"a": 1}, "rx_bytes": [1], "el_error": 0}
    resp = await client.post(f"/api/voice/web-call/{call_id}/client-report", headers=AUTH,
                             json={"stage": "track", "data": data})
    assert resp.status_code == 200
    line = _report_lines(caplog)[0]
    assert "el_error=0" in line and "secret" not in line and "tr_muted" not in line and "rx_bytes" not in line


@pytest.mark.asyncio
async def test_unknown_stage_is_400():
    client, call_id = await _started()
    resp = await client.post(f"/api/voice/web-call/{call_id}/client-report", headers=AUTH,
                             json={"stage": "later", "data": {}})
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_not_owned_call_is_404(caplog):
    client = _app(MemoryStore(), _room()).test_client()
    caplog.set_level(logging.INFO)
    resp = await client.post("/api/voice/web-call/deadbeef0123/client-report", headers=AUTH,
                             json={"stage": "track", "data": {}})
    assert resp.status_code == 404 and _report_lines(caplog) == []


@pytest.mark.asyncio
async def test_anonymous_is_refused(caplog):
    client, call_id = await _started()
    caplog.set_level(logging.INFO)
    resp = await client.post(f"/api/voice/web-call/{call_id}/client-report", json={"stage": "track", "data": {}})
    assert resp.status_code == 401 and _report_lines(caplog) == []


@pytest.mark.asyncio
async def test_oversized_body_is_413(caplog):
    client, call_id = await _started()
    caplog.set_level(logging.INFO)
    body = json.dumps({"stage": "track", "data": {"pc_state": "x" * 5000}})
    resp = await client.post(f"/api/voice/web-call/{call_id}/client-report",
                             headers={**AUTH, "Content-Type": "application/json", "Content-Length": str(len(body))},
                             data=body)
    assert resp.status_code == 413 and _report_lines(caplog) == []


def test_the_page_sends_the_three_stages_and_uses_keepalive():
    sent = set(re.findall(r'sendClientReport\("(\w+)"', HTML))
    assert sent == set(_CLIENT_REPORT_STAGES)
    fn = re.search(r"async function sendClientReport.*?\n      }\n", HTML, re.S).group(0)
    assert "keepalive: true" in fn and "await fetch" not in fn


def test_the_page_never_plays_the_remote_audio():
    assert not re.search(r"remoteAudio\s*\.\s*play\s*\(", HTML)
    assert ".play(" not in HTML


def test_the_page_keys_equal_the_server_allowlist():
    fn = re.search(r"async function sendClientReport.*?\n      }\n", HTML, re.S).group(0)
    keys = set(re.findall(r'put\("(\w+)"', fn)) | set(re.findall(r'take\("(\w+)"', fn))
    assert keys == set(_CLIENT_REPORT_KEYS)
    json.dumps(sorted(keys))
