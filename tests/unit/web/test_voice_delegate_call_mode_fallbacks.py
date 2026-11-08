"""/voice/delegate's call-mode read (LELIK_VOICE_MCP_RFC §8, O4), with the VOICE_MCP_TOOLS knob on: the mode decides
the allowlist, and anything that is not a readable, known mode counts as the plain companion call (`lelik`): a body
without a ticket, a missing record, a record without a mode or with an unknown one, and a store that fails (logged by
error type, never the ticket). With the knob off, no mode is read or passed. Plus the in-flight registry's handling of a
cancelled delegation task. Ids are invented.
"""
import asyncio
import logging
from typing import Optional
from unittest.mock import AsyncMock

import pytest
from quart import Quart

import src.web.voice_control_plane_app as voice_control_plane_app
from src.domain.voice_delegation_outcome import VoiceDelegationOutcome
from src.domain.voice_mode import VoiceMode
from src.ports.ephemeral_store import EphemeralStore
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.voice_tickets import DIGEST_KEY, call_kind_key

TICKET = "ticket-0001-invented"


class _Store(EphemeralStore):
    def __init__(self, records: Optional[dict] = None, fail: bool = False) -> None:
        self.records = records or {}
        self.fail = fail
        self.reads: list = []

    async def set(self, key, value, ttl_s):
        self.records[key] = value

    async def get(self, key):
        self.reads.append(key)
        if self.fail:
            raise ConnectionError("store unavailable")
        return self.records.get(key)

    async def delete(self, key):
        self.records.pop(key, None)

    async def get_and_delete(self, key):
        return self.records.pop(key, None)


async def _delegate(store: _Store, *, minting: bool = True, ticket: Optional[str] = TICKET):
    agent = AsyncMock()
    agent.delegate_outcome.return_value = VoiceDelegationOutcome("Garage space 14 behind 12 Oak Street", False)
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        lelik_agent_provider=AsyncMock(return_value=agent), digest_key=DIGEST_KEY, voice_mcp_minting=minting))
    body = {"user_id": "usr-1", "account_id": "acc-1", "arguments": {"intent": "search_memory", "query": "garage"},
            "call_context": []}
    if ticket is not None:
        body.update(ticket=ticket, call_id="call-1", request="search_memory: garage")
    response = await app.test_client().post("/voice/delegate", json=body, headers={"Authorization": "Bearer x"})
    assert response.status_code == 200
    return agent.delegate_outcome.await_args.kwargs


async def test_a_known_mode_in_the_record_is_passed_on():
    store = _Store({call_kind_key(TICKET): {"call_kind": "web", "mode": "interpreter"}})

    assert (await _delegate(store))["mode"] is VoiceMode.INTERPRETER


async def test_a_body_without_a_ticket_counts_as_the_companion_call_and_reads_nothing():
    store = _Store()

    kwargs = await _delegate(store, ticket=None)

    assert kwargs["mode"] is VoiceMode.LELIK
    assert store.reads == []


@pytest.mark.parametrize("record", [None, {"call_kind": "web"}, {"mode": "karaoke"}, {"mode": None}])
async def test_a_missing_modeless_or_unknown_record_counts_as_the_companion_call(record):
    store = _Store({call_kind_key(TICKET): record} if record is not None else {})

    assert (await _delegate(store))["mode"] is VoiceMode.LELIK


async def test_an_unreadable_record_counts_as_the_companion_call_and_logs_the_error_type_only(caplog):
    store = _Store(fail=True)
    caplog.set_level(logging.WARNING)

    kwargs = await _delegate(store)

    assert kwargs["mode"] is VoiceMode.LELIK
    warnings = [r.getMessage() for r in caplog.records if "call mode lookup" in r.getMessage()]
    assert len(warnings) == 1 and "ConnectionError" in warnings[0] and "treated as lelik" in warnings[0]
    assert TICKET not in warnings[0] and "store unavailable" not in warnings[0]


async def test_with_the_knob_off_no_mode_is_read_or_passed():
    store = _Store({call_kind_key(TICKET): {"mode": "interpreter"}})

    kwargs = await _delegate(store, minting=False)

    assert "mode" not in kwargs
    assert call_kind_key(TICKET) not in store.reads


async def test_a_cancelled_delegation_task_leaves_the_in_flight_registry_without_raising():
    task = asyncio.ensure_future(asyncio.sleep(3600))
    voice_control_plane_app._IN_FLIGHT_DELEGATIONS.add(task)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    voice_control_plane_app._forget_delegation(task)

    assert task not in voice_control_plane_app._IN_FLIGHT_DELEGATIONS
