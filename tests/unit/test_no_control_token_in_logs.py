"""The call control token reaches no log record (VOICE_CALL_CONTROL_CHANNEL_RFC §9): not on the main service's
mint and hand-out, not on the relay's `/ctl` paths (good hello, bad token, wrong version, oversized or malformed
frames, the caps, replace, teardown). A second check reads the source: no log call names a token."""
import ast
import asyncio
import json
import logging
from pathlib import Path

import pytest

from src.domain.secret_digest import fingerprint
from src.domain.voice_control_protocol import control_token_digest
from src.handlers.call_control_handler import CallControlHandler
from src.services.call_control_hub import CallControlHub
from tests.support.control_socket_fake import FakeControlSocket
from tests.unit.web.test_voice_web_call_control_handout import World

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "src/handlers/call_control_handler.py", "src/services/call_control_hub.py", "src/web/voice_web_call_app.py",
    "src/services/voice_session_service.py", "src/services/voice_call_setup_service.py",
)
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
TOKEN = "token-0001-invented-0123456789abcdefghijk"


def _names_a_token(node: ast.AST) -> bool:
    if isinstance(node, ast.Name) and "token" in node.id.lower():
        return True
    if isinstance(node, ast.Attribute) and "token" in node.attr.lower():
        return True
    if isinstance(node, ast.Constant) and isinstance(node.value, str) and "token" in node.value.lower():
        return False  # the word in prose is fine; a value is what must not appear
    return any(_names_a_token(child) for child in ast.iter_child_nodes(node))


def _log_calls(tree: ast.AST):
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in LOG_METHODS
                and isinstance(n.func.value, ast.Name) and n.func.value.id == "logger"):
            yield n


@pytest.mark.parametrize("rel", FILES)
def test_no_log_call_names_a_token(rel):
    calls = list(_log_calls(ast.parse((ROOT / rel).read_text())))
    assert calls, f"{rel}: no log call found; the walker is broken"
    assert [f"{rel}:{c.lineno}" for c in calls if any(_names_a_token(a) for a in c.args)] == []


def test_the_walker_flags_a_logged_token():
    assert any(_names_a_token(a) for c in _log_calls(ast.parse('logger.info(f"ctl {control_token}")')) for a in c.args)
    assert any(_names_a_token(a) for c in _log_calls(ast.parse('logger.info(f"ctl {frame.token}")')) for a in c.args)


def _text(caplog) -> str:
    assert caplog.records
    return "\n".join(r.getMessage() + (str(r.exc_info[1]) if r.exc_info else "") for r in caplog.records)


@pytest.mark.asyncio
async def test_main_service_mint_and_hand_out_log_no_token(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("VOICE_CONTROL_CHANNEL", "on")
    w = World()
    call_id = await w.start(mode="interpreter")
    token = (await w.connect(call_id))["control"]["token"]
    await w.client.post(f"/api/voice/web-call/{call_id}/hangup", headers={"Authorization": "Bearer tok"},
                        json={"trigger": "button"})
    text = _text(caplog)
    assert token not in text and control_token_digest(token) not in text


async def _socket(handler, *frames):
    page = FakeControlSocket()
    for frame in frames:
        page.page_says(frame)
    task = asyncio.ensure_future(handler.handle_connection(page))
    await asyncio.sleep(0.02)
    return page, task


@pytest.mark.asyncio
async def test_relay_control_paths_log_no_token(caplog):
    caplog.set_level(logging.DEBUG)
    hub = CallControlHub(send_timeout_s=0.05)
    handler = CallControlHandler(hub, hello_timeout_s=0.2, link_wait_s=0.05)
    link = hub.open(control_token_digest(TOKEN), fingerprint("ticket-0001-invented"))
    hello = {"type": "hello", "v": 1, "token": TOKEN}

    good, good_task = await _socket(handler, hello, {"type": "ping"}, {"type": "x", "token": TOKEN})
    replacing, replacing_task = await _socket(handler, hello)                         # replaces `good`
    await _socket(handler, {"type": "hello", "v": 1, "token": TOKEN + "-wrong"})      # bad token
    await _socket(handler, {"type": "hello", "v": 9.5, "token": TOKEN})               # bad version
    await _socket(handler, {"type": "hello", "v": 1, "token": TOKEN, "pad": "x" * 2000})  # oversized hello
    await _socket(handler, '{"type": "hello", "token": "' + TOKEN + '"')             # malformed JSON
    await _socket(handler, {"type": "ping", "token": TOKEN})                          # not a hello
    replacing.page_says(json.dumps({"type": "x", "token": TOKEN, "pad": "y" * 2000}))  # frame cap
    await asyncio.wait_for(replacing_task, timeout=2)
    capped, capped_task = await _socket(handler, hello, *([{"type": "ping", "token": TOKEN}] * 12))  # rate cap
    await asyncio.wait_for(capped_task, timeout=2)
    last, last_task = await _socket(handler, hello)
    last.stall_sends = True
    await hub.close(link)                                                             # teardown, stalled page
    await asyncio.wait_for(asyncio.gather(good_task, last_task), timeout=2)

    text = _text(caplog)
    assert "control" in text
    assert TOKEN not in text and control_token_digest(TOKEN) not in text
