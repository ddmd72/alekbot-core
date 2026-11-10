"""The voice MCP tools from prepare to the provider socket, and no token in any log line on the way
(LELIK_VOICE_MCP_RFC §4, §5.4, §11 S3).

The main side is real: `LelikAgent.session_config` with the servers (knob on), `VoiceCallSetupService.prepare`, the
control-plane blueprint minting at `/voice/session-config` with the real token service on the evaluating fake. The
relay side is real too: `VoiceSessionService` over the real `XaiRealtimeAdapter`, whose socket is a fake xAI that
answers the session.update with a failed tools/list and an error event echoing what it was sent, tokens included.
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.agents.lelik_agent import LelikAgent
from src.domain.lelik_context import LelikContext
from src.domain.voice_mcp_servers import VOICE_MCP_SERVERS_FIELD, VOICE_MCP_TOKENS_FIELD, voice_session_servers
from src.domain.voice_mode import VoiceMode
from src.domain.voice_playback_tracker import PlaybackTracker
from src.domain.voice_provider_profile import VOICE_PROVIDER_PROFILES
from src.infrastructure.agent_coordinator import AgentCoordinator
from src.infrastructure.agent_manifest import ALL_DESCRIPTORS
from src.infrastructure.agent_registry import AgentRegistry
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.services.voice_session_service import VoiceSessionService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.firestore_eval import eval_transactions
from tests.support.voice_mcp_kit import ACCOUNT, BASE, DIGEST_KEY, DRIVE_URI, FACTS_URI, TICKET, USER, Kit


class EchoingXai:
    """A fake xAI socket: after the session.update it reports the first MCP server's tools/list as failed (its error
    text carrying the URL and the token) and then an error event that echoes the session.update it got."""

    def __init__(self) -> None:
        self.sent: list = []
        self._script = None

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def _build_script(self) -> list:
        update = self.sent[0]
        mcp = [tool for tool in update["session"]["tools"] if tool["type"] == "mcp"]
        server = mcp[0]
        return [
            {"type": "mcp_list_tools.in_progress", "item_id": "item-mcp-1", "server_label": server["server_label"]},
            {"type": "mcp_list_tools.failed", "item_id": "item-mcp-1", "server_label": server["server_label"],
             "error": {"message": f"Cancelled by cancel scope ({server['server_url']}, "
                                  f"Authorization: Bearer {server['authorization']})"}},
            {"type": "error", "error": {"type": "invalid_request_error",
                                        "message": f"authorization {server['authorization']} refused",
                                        "echo": json.dumps(update)}},
        ]

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._script is None:
            self._script = self._build_script()
        if not self._script:
            raise StopAsyncIteration
        return json.dumps(self._script.pop(0))

    async def close(self) -> None:
        pass


def _lelik() -> LelikAgent:
    prompt_builder = AsyncMock()
    prompt_builder.build_for_agent.return_value = "you are Lelik"
    persona = AsyncMock()
    persona.assemble.return_value = LelikContext(biographical_facts=[], conversation_history=[])
    agent = LelikAgent(
        config=MagicMock(agent_id="lelik_agent_usr-1"), telephony=AsyncMock(),
        from_number="+1", status_callback_url="https://x/voice/status",
        prompt_builder=prompt_builder, persona=persona, notifications=AsyncMock(),
        voice_profile=VOICE_PROVIDER_PROFILES["xai"], mcp_servers=voice_session_servers(FACTS_URI),
    )
    registry = AgentRegistry()
    for descriptor in ALL_DESCRIPTORS:
        registry.register(descriptor)
    agent.coordinator = AgentCoordinator(registry=registry, task_queue=None, alert_sink=AsyncMock())
    return agent


class Alerts:
    def __init__(self) -> None:
        self.posted: list = []

    async def post(self, text: str) -> None:
        self.posted.append(text)


async def _session_config(kit: Kit) -> dict:
    """prepare + /voice/session-config on the main side, as the relay receives it."""
    setup = VoiceCallSetupService(kit.store, kit.alerts, AsyncMock(return_value=_lelik()), digest_key=DIGEST_KEY)
    await setup.prepare(TICKET, USER, ACCOUNT, "web", mode=VoiceMode.LELIK)
    stored = json.dumps(kit.docs, default=str)
    assert "eyJ" not in stored and "authorization" not in stored        # the ticket record holds descriptors only
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=kit.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=kit.alerts,
        digest_key=DIGEST_KEY, voice_mcp_tokens=kit.voice, voice_mcp_minting=True))
    response = await app.test_client().post("/voice/session-config", json={"ticket": TICKET})
    assert response.status_code == 200
    return await response.get_json()


async def _run_relay(config: dict, connect) -> tuple:
    control = AsyncMock()
    control.fetch_session_config.return_value = config
    alerts = Alerts()
    service = VoiceSessionService(
        realtime_session_factory=lambda spec: XaiRealtimeAdapter(api_key="xai-test", ws_connect=connect),
        control_plane=control, alert_sink=alerts)

    async def inbound():
        await asyncio.sleep(0.3)
        return
        yield  # pragma: no cover

    try:
        await service.handle_call(ticket=TICKET, inbound_audio=inbound(), send_outbound_audio=AsyncMock(),
                                  clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())
    except Exception:
        pass
    return control, alerts


def _everything_logged(caplog) -> str:
    parts = []
    for record in caplog.records:
        parts.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            parts.append(repr(record.exc_info[1]))
        if record.exc_text:
            parts.append(record.exc_text)
    return "\n".join(parts)


@pytest.fixture
def transactions():
    with eval_transactions():
        yield


@pytest.mark.usefixtures("transactions")
class TestNoTokenInAnyLog:
    async def test_the_full_path_offers_the_drive_server_and_logs_no_token(self, caplog):
        kit = Kit()
        caplog.set_level(logging.DEBUG)
        config = await _session_config(kit)
        assert set(config[VOICE_MCP_TOKENS_FIELD]) == {"drive"}
        tokens = [config[VOICE_MCP_TOKENS_FIELD]["drive"]]
        assert all(tokens) and config[VOICE_MCP_SERVERS_FIELD]
        ws = EchoingXai()

        control, alerts = await _run_relay(config, AsyncMock(return_value=ws))

        # The session.update carried the drive server with its raw token (the test is not vacuous).
        mcp = [tool for tool in ws.sent[0]["session"]["tools"] if tool["type"] == "mcp"]
        assert [(t["server_label"], t["authorization"]) for t in mcp] == [("alek_drive", tokens[0])]
        logged = _everything_logged(caplog)
        reported = "\n".join(alerts.posted + kit.alerts.posted) + repr(control.mock_calls)
        for token in tokens:
            assert token not in logged
            assert token not in reported
        assert BASE.split("//")[1] not in logged
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("remote tool server alek_drive unavailable" in w for w in warnings)
        assert alerts.posted and "invalid_request_error" in alerts.posted[0]

    async def test_a_failed_open_whose_error_carries_the_token_logs_no_token(self, caplog):
        kit = Kit()
        caplog.set_level(logging.DEBUG)
        config = await _session_config(kit)
        token = config[VOICE_MCP_TOKENS_FIELD]["drive"]
        connect = AsyncMock(side_effect=OSError(f"handshake failed: Authorization: Bearer {token} {DRIVE_URI}"))

        await _run_relay(config, connect)

        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "failed to open realtime session: OSError" in logged
        assert token not in logged and DRIVE_URI not in logged

    async def test_the_relay_hands_no_token_to_the_control_plane(self, caplog):
        kit = Kit()
        config = await _session_config(kit)
        tokens = list(config[VOICE_MCP_TOKENS_FIELD].values())

        control, _ = await _run_relay(config, AsyncMock(return_value=EchoingXai()))

        control.submit_transcript.assert_awaited_once()
        assert all(token not in repr(control.mock_calls) for token in tokens)


class TestNoServersWithoutTokens:
    async def test_descriptors_without_tokens_open_no_mcp_entry(self):
        config = {"instructions": "you are Lelik", "tools": [], "user_id": USER, "account_id": ACCOUNT,
                  "mode": "lelik", "call_kind": "web",
                  "voice": {"provider": "xai", "turn_ownership": "provider", "voice": "castor",
                            "reasoning_effort": "high"},
                  VOICE_MCP_SERVERS_FIELD: [s.to_dict() for s in voice_session_servers(FACTS_URI)]}
        ws = EchoingXai()
        ws._script = []

        await _run_relay(config, AsyncMock(return_value=ws))

        assert all(tool["type"] != "mcp" for tool in ws.sent[0]["session"].get("tools", []))
