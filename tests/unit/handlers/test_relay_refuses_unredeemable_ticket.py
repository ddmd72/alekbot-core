"""The relay side of the ticket gate (P7, inventory §4 "Relay").

A Twilio media stream whose ticket is unknown or already redeemed ends the call before any realtime session opens and
before anything is billed: the relay's control-plane client posts to the REAL `/voice/session-config` route (over an
in-process ASGI transport), which consumes tickets from the real Firestore ephemeral store on the evaluating fake.
An SFU socket on a path the relay does not serve is closed without registering a call
(`src/handlers/sfu_stream_handler.py:93-98`).
"""
import json

import httpx
import pytest
from quart import Quart

from src.adapters.firestore_ephemeral_store import FirestoreEphemeralStore
from src.adapters.http_call_control_plane_adapter import HttpCallControlPlaneAdapter
from src.handlers.media_stream_handler import MediaStreamHandler
from src.handlers.sfu_stream_handler import SfuStreamHandler
from src.services.voice_session_service import VoiceSessionService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.firestore_eval import EvalDb, eval_transactions
from tests.unit.web.gate_fakes import Tripwire
from tests.support.voice_tickets import ticket_key

MAIN = "https://main.example.test"
RELAY_TOKEN = "relay-oidc-token"


class FakeTwilioWs:
    def __init__(self, ticket: str) -> None:
        self._messages = [
            {"event": "start", "start": {"streamSid": "MZ1", "customParameters": {"ticket": ticket}}},
            {"event": "media", "media": {"payload": "AAAA"}},
        ]
        self.sent: list = []
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return json.dumps(self._messages.pop(0))

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))

    async def close(self) -> None:
        self.closed = True


class World:
    def __init__(self) -> None:
        self.db = EvalDb()
        self.touched: list = []
        self.sessions_opened: list = []
        self.store = FirestoreEphemeralStore(self.db, collection="voice_tickets")

        async def relay_only(authorization):     # the OIDC check itself is covered by the control-plane tests
            return authorization == f"Bearer {RELAY_TOKEN}"

        main = Quart("main_service")
        main.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.store, quota_service=Tripwire("quota_service", self.touched),
            prompt_content_store=Tripwire("prompt_content_store", self.touched),
            summary_consumer=Tripwire("summary_consumer", self.touched).consume,
            oidc_verifier=relay_only, alert_sink=Tripwire("alert_sink", self.touched), digest_key=b"k" * 32
        ))
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=main), base_url=MAIN)
        control_plane = HttpCallControlPlaneAdapter(MAIN, id_token_provider=lambda: RELAY_TOKEN, http_client=self.http)

        def open_session(spec):
            self.sessions_opened.append(spec)
            raise AssertionError("a realtime session must not be built for an unredeemable ticket")

        self.handler = MediaStreamHandler(VoiceSessionService(
            realtime_session_factory=open_session, control_plane=control_plane,
            alert_sink=Tripwire("relay_alert_sink", self.touched)))

    async def stream(self, ticket: str) -> FakeTwilioWs:
        ws = FakeTwilioWs(ticket)
        with pytest.raises(httpx.HTTPStatusError) as raised:
            await self.handler.handle_connection(ws)
        assert raised.value.response.status_code == 404
        return ws


async def test_unknown_ticket_ends_the_call_before_a_session_opens():
    w = World()
    with eval_transactions():
        ws = await w.stream("never-minted")
    assert ws.closed
    assert w.sessions_opened == [] and w.touched == []


async def test_redeemed_ticket_cannot_start_a_second_call():
    w = World()
    with eval_transactions():
        await w.store.set(ticket_key("t1"), {"user_id": "u1", "account_id": "a1", "instructions": "x"}, ttl_s=300)
        first = await w.http.post("/voice/session-config", json={"ticket": "t1"},
                                  headers={"Authorization": f"Bearer {RELAY_TOKEN}"})
        assert first.status_code == 200              # the legitimate relay redeemed it
        ws = await w.stream("t1")                    # a replay of the same ticket
    assert ws.closed
    assert w.sessions_opened == [] and w.touched == []
    assert w.db.collection("voice_tickets").docs == {}


# --------------------------------------------------------------------------- SFU paths

class FakeSfuWs:
    def __init__(self) -> None:
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def close(self) -> None:
        self.closed = True


@pytest.mark.parametrize("path", [
    "/sfu/other?ticket=3f2c9d5e-0d4b-4b8e-9a51-1c2d3e4f5a6b",   # not ingest/egress
    "/sfu/ingest",                                             # no ticket
    "/sfu/egress?ticket=",                                     # empty ticket
    "/media?ticket=3f2c9d5e-0d4b-4b8e-9a51-1c2d3e4f5a6b",        # not under /sfu/
])
async def test_unparsable_sfu_path_is_closed_and_registers_nothing(path):
    touched: list = []
    handler = SfuStreamHandler(Tripwire("voice_session_service", touched))
    ws = FakeSfuWs()
    await handler.handle_connection(ws, path)
    assert ws.closed
    assert handler._calls == {} and touched == []
