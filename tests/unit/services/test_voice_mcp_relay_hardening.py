"""Hardening after the S3 security review (LELIK_VOICE_MCP_RFC §5.4, §8): tokens scrubbed by value, each token bound to
its server URL, uniform redaction, and the call end reported (so the tokens are revoked) on an early failure.

Tokens are real HS256 JWTs signed with an invented test key; ids and URLs are invented (`usr-1`, `mcp.example.test`).
"""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock

import jwt
import pytest

from src.adapters.xai_realtime_adapter import XaiRealtimeAdapter
from src.domain.log_redaction import redact_exception, redact_for_log, redact_traceback
from src.domain.remote_tool_server import RemoteToolServer
from src.domain.voice_mcp_servers import (
    VOICE_MCP_SERVERS_FIELD,
    VOICE_MCP_TOKENS_FIELD,
    pair_session_servers,
    unverified_audience,
    voice_session_servers,
)
from src.domain.voice_playback_tracker import PlaybackTracker
from src.handlers.media_stream_handler import MediaStreamHandler
from src.handlers.sfu_stream_handler import SfuStreamHandler
from src.services.voice_session_service import VoiceSessionService

_KEY = "invented-test-signing-key-0123456789abcdef-0123456789"
FACTS_URI = "https://mcp.example.test/mcp"
DRIVE_URI = "https://mcp.example.test/drive-mcp"
XAI = {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}


def _token(aud: str, sub: str = "usr-1") -> str:
    return jwt.encode({"sub": sub, "aud": aud, "type": "voice_mcp_access", "cid": "c" * 64}, _KEY, algorithm="HS256",
                      headers={"kid": "voice-mcp"})


FACTS_TOKEN = _token(f"voice:{FACTS_URI}")
DRIVE_TOKEN = _token(f"voice:{DRIVE_URI}")


def _config(tokens=None, descriptors=None, **over) -> dict:
    config = {"instructions": "you are Lelik", "tools": [], "user_id": "usr-1", "account_id": "acc-1",
              "mode": "lelik", "call_kind": "web", "voice": XAI,
              VOICE_MCP_SERVERS_FIELD: descriptors if descriptors is not None else
              [s.to_dict() for s in voice_session_servers(FACTS_URI)],
              VOICE_MCP_TOKENS_FIELD: tokens if tokens is not None else {"facts": FACTS_TOKEN, "drive": DRIVE_TOKEN}}
    config.update(over)
    return config


def _logged(caplog) -> str:
    return "\n".join(r.getMessage() + (repr(r.exc_info[1]) if r.exc_info else "") for r in caplog.records)


class TestScrubByValue:
    def test_payload_and_signature_without_the_header_are_redacted(self):
        _, payload, signature = FACTS_TOKEN.split(".")

        out = redact_for_log(f"echo {payload}.{signature} end", secrets=[FACTS_TOKEN])

        assert payload not in out and signature not in out and out.startswith("echo ") and out.endswith(" end")

    def test_a_bare_signature_is_redacted_by_value(self):
        signature = FACTS_TOKEN.split(".")[2]

        out = redact_for_log(f"sig={signature}", secrets=[FACTS_TOKEN])

        assert signature not in out

    def test_a_full_token_is_redacted(self):
        assert FACTS_TOKEN not in redact_for_log(f"token {FACTS_TOKEN}", secrets=[FACTS_TOKEN])

    def test_a_headerless_pair_is_caught_by_the_backstop_without_the_secret(self):
        _, payload, signature = DRIVE_TOKEN.split(".")

        out = redact_for_log(f"value {payload}.{signature}")

        assert payload not in out and signature not in out

    @pytest.mark.parametrize("text", [
        "Cancelled by cancel scope",
        "voice call 1e63c791: remote tool server alek_memory unavailable",
        "Garage space 14 behind 12 Oak Street",
        "src/services/voice_session_service.py, line 260, in _run_call",
    ])
    def test_normal_text_stays_readable(self, text):
        assert redact_for_log(text, secrets=[FACTS_TOKEN, DRIVE_TOKEN]) == text

    def test_an_exception_and_its_traceback_are_redacted(self):
        _, payload, signature = FACTS_TOKEN.split(".")
        try:
            raise RuntimeError(f"refused {payload}.{signature}")
        except RuntimeError as exc:
            line, trace = redact_exception(exc, secrets=[FACTS_TOKEN]), redact_traceback(exc, secrets=[FACTS_TOKEN])

        assert line.startswith("RuntimeError: refused ")
        assert "Traceback" in trace and "RuntimeError" in trace
        assert all(part not in line + trace for part in (payload, signature))


class TestAdapterScrubsTheCallsTokens:
    async def test_a_headerless_echo_in_an_error_and_a_failed_list_is_scrubbed(self, caplog):
        _, payload, signature = FACTS_TOKEN.split(".")

        class Ws:
            sent: list = []

            async def send(self, raw):
                self.sent.append(raw)

            def __aiter__(self):
                self._events = iter([
                    {"type": "mcp_list_tools.failed", "server_label": "alek_memory", "error": f"bad {signature}"},
                    {"type": "error", "error": {"message": f"echo {payload}.{signature}"}},
                ])
                return self

            async def __anext__(self):
                try:
                    return json.dumps(next(self._events))
                except StopIteration:
                    raise StopAsyncIteration

            async def close(self):
                pass

        server = RemoteToolServer("alek_memory", FACTS_URI, "memory", ("get_user_context",), token=FACTS_TOKEN)
        adapter = XaiRealtimeAdapter(api_key="xai-test", ws_connect=AsyncMock(return_value=Ws()))
        await adapter.open(instructions="hi", reasoning_effort="high", tools=[], mcp_servers=[server])
        caplog.set_level(logging.DEBUG)

        failed, error = [event async for event in adapter.receive_events()]

        text = _logged(caplog) + failed.payload["reason"] + error.payload["message"]
        assert payload not in text and signature not in text
        assert failed.payload["reason"].startswith("bad ") and "echo <redacted>" in error.payload["message"]


class TestTokenBoundToItsServer:
    def test_the_audience_is_read_without_the_key(self):
        assert unverified_audience(FACTS_TOKEN) == f"voice:{FACTS_URI}"
        assert unverified_audience("not-a-jwt") is None
        assert unverified_audience("a.%%%.c") is None

    def test_matching_audiences_offer_both_servers(self):
        servers, dropped = pair_session_servers(_config())

        assert [(s.label, s.token) for s in servers] == [("alek_memory", FACTS_TOKEN), ("alek_drive", DRIVE_TOKEN)]
        assert dropped == ()

    def test_a_mismatched_audience_drops_that_server(self):
        servers, dropped = pair_session_servers(_config(tokens={"facts": DRIVE_TOKEN, "drive": DRIVE_TOKEN}))

        assert [s.label for s in servers] == ["alek_drive"]
        assert len(dropped) == 1 and dropped[0].startswith("alek_memory")
        assert DRIVE_TOKEN not in dropped[0]

    def test_a_descriptor_pointing_elsewhere_drops_that_server(self):
        descriptors = [s.to_dict() for s in voice_session_servers(FACTS_URI)]
        descriptors[0]["url"] = "https://elsewhere.example.test/mcp"

        servers, dropped = pair_session_servers(_config(descriptors=descriptors))

        assert [s.label for s in servers] == ["alek_drive"] and dropped[0].startswith("alek_memory")

    def test_a_duplicate_label_drops_the_duplicate(self):
        descriptors = [s.to_dict() for s in voice_session_servers(FACTS_URI)]
        descriptors.append({**descriptors[0], "url": "https://elsewhere.example.test/mcp"})

        servers, dropped = pair_session_servers(_config(descriptors=descriptors))

        assert [(s.label, s.url) for s in servers] == [("alek_memory", FACTS_URI), ("alek_drive", DRIVE_URI)]
        assert dropped == ("alek_memory (duplicate)",)


class _RecordingWs:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        pass


async def _inbound():
    await asyncio.sleep(0.2)
    return
    yield  # pragma: no cover


def _service(control, factory):
    return VoiceSessionService(realtime_session_factory=factory, control_plane=control, alert_sink=AsyncMock())


async def _handle(service):
    await service.handle_call(ticket="ticket-0001-invented", inbound_audio=_inbound(), send_outbound_audio=AsyncMock(),
                              clear_outbound_audio=AsyncMock(), playback=PlaybackTracker())


class TestRelayBindsAndReports:
    async def test_a_mismatched_server_is_dropped_with_a_warning_naming_only_its_label(self, caplog):
        control = AsyncMock()
        control.fetch_session_config.return_value = _config(tokens={"facts": DRIVE_TOKEN, "drive": DRIVE_TOKEN})
        ws = _RecordingWs()
        caplog.set_level(logging.DEBUG)

        await _handle(_service(control, lambda spec: XaiRealtimeAdapter(api_key="x", ws_connect=AsyncMock(
            return_value=ws))))

        assert [t["server_label"] for t in ws.sent[0]["session"]["tools"] if t["type"] == "mcp"] == ["alek_drive"]
        warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
        assert any("remote tool server alek_memory" in w and "dropped" in w for w in warnings)
        assert DRIVE_TOKEN not in _logged(caplog) and "mcp.example.test" not in "\n".join(warnings)

    async def test_a_factory_that_raises_still_reports_the_call_end(self):
        control = AsyncMock()
        control.fetch_session_config.return_value = _config()

        def factory(spec):
            raise RuntimeError("no realtime provider configured")

        with pytest.raises(RuntimeError):
            await _handle(_service(control, factory))

        control.submit_transcript.assert_awaited_once()
        assert control.submit_transcript.await_args.kwargs["call_id"] == "ticket-0001-invented"

    async def test_an_unusable_config_still_reports_the_call_end_and_logs_no_token(self, caplog):
        control = AsyncMock()
        broken = _config(voice={"provider": "xai"})          # VoiceSessionSpec.from_dict raises KeyError
        control.fetch_session_config.return_value = broken
        caplog.set_level(logging.DEBUG)

        with pytest.raises(KeyError):
            await _handle(_service(control, MagicMock()))

        control.submit_transcript.assert_awaited_once()
        assert control.submit_transcript.await_args.kwargs["user_id"] == "usr-1"
        assert FACTS_TOKEN not in _logged(caplog) and DRIVE_TOKEN not in _logged(caplog)
        assert "session config unusable: KeyError" in _logged(caplog)


class TestHandlersLogARedactedTraceback:
    async def test_the_media_stream_handler_logs_redacted_text_without_exc_info(self, caplog):
        class BrokenWs:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError(f"stream broke at {FACTS_URI} with {FACTS_TOKEN}")

        caplog.set_level(logging.DEBUG)

        with pytest.raises(RuntimeError):
            await MediaStreamHandler(MagicMock()).handle_connection(BrokenWs())

        [record] = [r for r in caplog.records if "media stream" in r.getMessage() and "failed" in r.getMessage()]
        assert record.exc_info is None
        assert "Traceback" in record.getMessage() and "RuntimeError" in record.getMessage()
        assert FACTS_TOKEN not in record.getMessage() and FACTS_URI not in record.getMessage()

    async def test_the_sfu_stream_handler_logs_redacted_text_without_exc_info(self, caplog):
        ticket = "5f0c1a3e-8d2b-4c1f-9a7e-2b6d4e8f1a03"

        class Service:
            async def handle_call(self, **kw):
                raise RuntimeError(f"open failed: Authorization: Bearer {FACTS_TOKEN} at {FACTS_URI}")

        class SfuWs:
            def __init__(self):
                self.incoming: asyncio.Queue = asyncio.Queue()

            def __aiter__(self):
                return self

            async def __anext__(self):
                item = await self.incoming.get()
                if item is None:
                    raise StopAsyncIteration
                return item

            async def send(self, data):
                pass

            async def close(self):
                self.incoming.put_nowait(None)

        handler = SfuStreamHandler(Service(), frame_interval_s=0.001, egress_reattach_s=0.05)
        ingest, egress = SfuWs(), SfuWs()
        caplog.set_level(logging.DEBUG)

        await asyncio.wait_for(asyncio.gather(
            handler.handle_connection(ingest, f"/sfu/ingest?ticket={ticket}"),
            handler.handle_connection(egress, f"/sfu/egress?ticket={ticket}"),
            return_exceptions=True), timeout=3)

        [record] = [r for r in caplog.records if "call failed" in r.getMessage()]
        assert record.exc_info is None
        assert "Traceback" in record.getMessage() and "RuntimeError" in record.getMessage()
        assert FACTS_TOKEN not in record.getMessage() and FACTS_URI not in record.getMessage()
