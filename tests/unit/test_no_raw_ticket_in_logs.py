"""A voice ticket reaches no log record raw (SECRETS_AT_REST_RFC §2.5, S4): logs carry `fingerprint(ticket)`."""
import ast
import asyncio
import logging
import re
from pathlib import Path

import pytest

from src.handlers.sfu_stream_handler import SfuStreamHandler
from tests.support.firestore_eval import eval_transactions
from tests.unit.handlers.test_relay_refuses_unredeemable_ticket import World as RelayWorld
from tests.unit.handlers.test_sfu_stream_handler import FakeSessionService, FakeSfuWs
from tests.unit.web.test_voice_ticket_digest import World as MainWorld

ROOT = Path(__file__).resolve().parents[2]
FILES = (
    "src/web/voice_webhook_app.py", "src/web/voice_web_call_app.py", "src/web/voice_control_plane_app.py",
    "src/services/voice_call_setup_service.py", "src/services/voice_session_service.py",
    "src/handlers/sfu_stream_handler.py", "src/handlers/media_stream_handler.py",
    "src/adapters/http_call_control_plane_adapter.py",
)
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical"}
UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
T1 = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"


def _outside_fingerprint(node: ast.AST) -> bool:
    """True when a name containing `ticket` occurs in `node` outside a `fingerprint(...)` call."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "fingerprint":
        return False
    if isinstance(node, ast.Name) and "ticket" in node.id.lower():
        return True
    if isinstance(node, ast.Attribute) and "ticket" in node.attr.lower():
        return True
    return any(_outside_fingerprint(child) for child in ast.iter_child_nodes(node))


def _log_calls(tree: ast.AST):
    for n in ast.walk(tree):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in LOG_METHODS
                and isinstance(n.func.value, ast.Name) and n.func.value.id in {"logger", "log"}):
            yield n
        # an alert posts to an ops channel, which is as public as a log
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "post"
                and isinstance(n.func.value, ast.Attribute) and "alert" in n.func.value.attr.lower()):
            yield n


@pytest.mark.parametrize("rel", FILES)
def test_log_calls_print_a_ticket_only_as_a_fingerprint(rel):
    tree = ast.parse((ROOT / rel).read_text())
    calls = list(_log_calls(tree))
    assert calls, f"{rel}: no log call found; the walker is broken"
    offenders = [f"{rel}:{c.lineno}" for c in calls if any(_outside_fingerprint(a) for a in c.args)]
    assert offenders == []


def test_the_walker_flags_a_raw_ticket():
    bad = ast.parse('logger.info(f"call {ticket}")')
    good = ast.parse('logger.info(f"call {fingerprint(state.ticket)}")')
    assert [c for c in _log_calls(bad) if any(_outside_fingerprint(a) for a in c.args)]
    assert not [c for c in _log_calls(good) if any(_outside_fingerprint(a) for a in c.args)]


def _no_ticket_in(caplog, *tickets):
    text = "\n".join(r.getMessage() + (str(r.exc_info[1]) if r.exc_info else "") for r in caplog.records)
    assert caplog.records
    for ticket in tickets:
        assert ticket not in text


@pytest.mark.asyncio
async def test_phone_and_web_setup_log_no_ticket(caplog):
    caplog.set_level(logging.DEBUG)
    with eval_transactions():
        w = MainWorld()
        await w.client.post("/voice/auth", form={"From": "+15550100", "CallSid": "CA0001"})
        await w.client.post("/voice/inbound-status", form={"CallSid": "CA0001", "CallStatus": "completed"})
        phone = w.agent.execute.await_args.kwargs["ticket"]
        await w.client.post(f"/voice/answer?ticket={phone}", form={})
        await w.client.post(f"/voice/status?ticket={phone}", form={"CallStatus": "completed"})
        await w.client.post("/voice/session-config", json={"ticket": phone})
        await w.client.post("/voice/session-config", json={"ticket": "ticket-a1"})
        w.agent.session_config.side_effect = RuntimeError("no prompt")
        await w.client.post(f"/voice/answer?ticket={phone}", form={})
        await w.client.post("/voice/delegate/abandon", json={"ticket": phone, "call_id": "CA0002"})
    _no_ticket_in(caplog, phone, "ticket-a1")


@pytest.mark.asyncio
async def test_web_call_setup_failure_logs_no_ticket(caplog):
    caplog.set_level(logging.DEBUG)
    with eval_transactions():
        w = MainWorld()
        w.media.open_caller.side_effect = RuntimeError("sfu down")
        resp = await w.client.post("/api/voice/web-call", json={"sdp": "OFFER", "mid": "0"},
                                   headers={"Authorization": "Bearer tok"})
    assert resp.status_code == 502
    _no_ticket_in(caplog)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert not UUID.search(text)      # the ticket is a uuid4 minted inside the route


@pytest.mark.asyncio
async def test_relay_logs_no_ticket(caplog):
    caplog.set_level(logging.DEBUG)
    # media stream: an unknown ticket fails inside the handler and logs
    w = RelayWorld()
    with eval_transactions():
        await w.stream("ticket-a1")
    # sfu: a call whose session raises logs "call failed" and "call closed"
    async def boom(_record):
        raise RuntimeError("provider down")

    handler = SfuStreamHandler(FakeSessionService(boom), frame_interval_s=0.001, egress_reattach_s=0.05)
    ingest, egress = FakeSfuWs(), FakeSfuWs()
    tasks = [asyncio.ensure_future(handler.handle_connection(ingest, f"/sfu/ingest?ticket={T1}")),
             asyncio.ensure_future(handler.handle_connection(egress, f"/sfu/egress?ticket={T1}"))]
    await asyncio.sleep(0.05)
    await egress.close()
    await ingest.close()
    await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=2)
    _no_ticket_in(caplog, "ticket-a1", T1)
