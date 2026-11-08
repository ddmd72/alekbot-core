"""`/voice/session-config` mints a call's voice MCP tokens, `/voice/submit-transcript` revokes them
(LELIK_VOICE_MCP_RFC §5.2-§5.4, §11 S1).

Minting happens at redemption only, with the VOICE_MCP_TOOLS knob on, for the plain companion call on xAI; the tokens
leave in the response field `voice_mcp_tokens` and nowhere else; the ticket record never holds one. The real blueprint,
the real `VoiceCallSetupService.prepare`, the real token service and the real ephemeral store on the evaluating fake.
"""
import json
import logging
from typing import Optional
from unittest.mock import AsyncMock, MagicMock

import pytest
from quart import Quart

from src.domain.mcp_resource import MCPResource
from src.domain.voice_mcp_token import voice_call_id, voice_call_key
from src.domain.voice_mode import VoiceMode
from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_control_plane_app import VOICE_MCP_TOKENS_FIELD, create_voice_control_plane_blueprint
from tests.support.firestore_eval import eval_transactions
from tests.support.mcp_gate_fakes import FakeUserRepo
from tests.support.voice_mcp_kit import ACCOUNT, DIGEST_KEY, TICKET, USER, Kit

XAI = {"provider": "xai", "turn_ownership": "provider", "voice": "castor", "reasoning_effort": "high"}
OPENAI = {"provider": "openai", "turn_ownership": "relay", "voice": "verse", "reasoning_effort": "medium"}


class World:
    def __init__(self, *, minting: bool = True, voice_service: bool = True, kit: Optional[Kit] = None) -> None:
        self.kit = kit or Kit()
        self.agent = MagicMock()
        self.session = {"instructions": "you are Lelik", "tools": [], "voice": XAI}
        self.agent.session_config = AsyncMock(side_effect=lambda **kw: dict(self.session))
        self.setup = VoiceCallSetupService(self.kit.store, self.kit.alerts, AsyncMock(return_value=self.agent),
                                           digest_key=DIGEST_KEY)
        app = Quart(__name__)
        app.register_blueprint(create_voice_control_plane_blueprint(
            ephemeral_store=self.kit.store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
            summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=self.kit.alerts,
            digest_key=DIGEST_KEY, voice_mcp_tokens=self.kit.voice if voice_service else None,
            voice_mcp_minting=minting))
        self.client = app.test_client()

    async def prepare(self, mode: VoiceMode = VoiceMode.LELIK, voice: Optional[dict] = XAI) -> None:
        if voice is None:
            self.session.pop("voice", None)
        else:
            self.session["voice"] = voice
        await self.setup.prepare(TICKET, USER, ACCOUNT, "web", mode=mode)

    async def session_config(self):
        return await self.client.post("/voice/session-config", json={"ticket": TICKET})

    async def submit(self):
        return await self.client.post("/voice/submit-transcript", json={
            "call_id": TICKET, "user_id": USER, "account_id": ACCOUNT, "transcript_text": "hello", "turns": []})

    def dump(self) -> str:
        return json.dumps(self.kit.docs, default=str)


@pytest.fixture
def transactions():
    with eval_transactions():
        yield


@pytest.mark.usefixtures("transactions")
class TestSessionConfigMints:
    async def test_the_companion_call_on_xai_gets_two_tokens_that_verify(self):
        w = World()
        await w.prepare()
        resp = await w.session_config()
        body = await resp.get_json()

        assert resp.status_code == 200
        tokens = body[VOICE_MCP_TOKENS_FIELD]
        assert set(tokens) == {"facts", "drive"}
        assert (await w.kit.voice.verify(tokens["facts"], MCPResource.FACTS)).user_id == USER
        assert (await w.kit.voice.verify(tokens["drive"], MCPResource.DRIVE)).account_id == ACCOUNT
        assert {k: v for k, v in body.items() if k != VOICE_MCP_TOKENS_FIELD}["instructions"] == "you are Lelik"

    async def test_the_ticket_record_never_holds_a_token(self):
        w = World()
        await w.prepare()
        before = w.dump()
        assert "voice_mcp" not in before and "eyJ" not in before        # no JWT at prepare
        body = await (await w.session_config()).get_json()
        after = w.dump()
        for token in body[VOICE_MCP_TOKENS_FIELD].values():
            assert token not in after
        assert json.loads(after)[voice_call_key(voice_call_id(DIGEST_KEY, TICKET))]["value"] == {"user_id": USER}

    @pytest.mark.parametrize("mode, voice", [
        pytest.param(VoiceMode.INTERPRETER, XAI, id="interpreter"),
        pytest.param(VoiceMode.TUTOR, XAI, id="tutor"),
        pytest.param(VoiceMode.LELIK, OPENAI, id="openai"),
        pytest.param(VoiceMode.LELIK, None, id="legacy-no-voice"),
    ])
    async def test_other_calls_get_no_tokens_and_no_record(self, mode, voice):
        w = World()
        await w.prepare(mode=mode, voice=voice)
        body = await (await w.session_config()).get_json()
        assert VOICE_MCP_TOKENS_FIELD not in body
        assert "voice_mcp_call" not in w.dump()

    async def test_the_knob_off_mints_nothing_writes_nothing_and_answers_as_before(self):
        w = World(minting=False)
        await w.prepare()
        stored = dict(w.kit.docs)
        body = await (await w.session_config()).get_json()
        assert VOICE_MCP_TOKENS_FIELD not in body
        assert set(body) == {"instructions", "tools", "voice", "user_id", "account_id", "call_kind", "mode"}
        assert set(w.kit.docs) == set(stored) - {k for k in stored if k.startswith("voice_ticket:")}

    async def test_no_key_mints_nothing_even_with_the_knob_on(self):
        w = World(voice_service=False)
        await w.prepare()
        body = await (await w.session_config()).get_json()
        assert VOICE_MCP_TOKENS_FIELD not in body and "voice_mcp_call" not in w.dump()

    async def test_a_user_the_whitelist_now_refuses_gets_the_call_without_tokens_and_an_alert(self):
        w = World()
        await w.prepare()
        w.kit.voice._user_repo = FakeUserRepo({})
        resp = await w.session_config()
        body = await resp.get_json()
        assert resp.status_code == 200 and body["instructions"] == "you are Lelik"
        assert VOICE_MCP_TOKENS_FIELD not in body and "voice_mcp_call" not in w.dump()
        assert any("Voice MCP: no memory or drive tools" in a and "no_user" in a for a in w.kit.alerts.posted)

    async def test_a_store_failure_at_mint_fails_the_session_config(self, caplog):
        w = World()
        await w.prepare()
        inner_set = w.kit.store.inner.set

        async def failing_set(key, value, ttl_s):
            if key.startswith("voice_mcp_call:"):
                raise RuntimeError("firestore down")
            await inner_set(key, value, ttl_s)
        w.kit.store.inner.set = failing_set
        resp = await w.session_config()
        assert resp.status_code == 500
        assert any("voice MCP token mint failed" in r.getMessage() and "RuntimeError" in r.getMessage()
                   for r in caplog.records)


@pytest.mark.usefixtures("transactions")
class TestSubmitTranscriptRevokes:
    async def test_after_submit_both_tokens_fail(self):
        w = World()
        await w.prepare()
        tokens = (await (await w.session_config()).get_json())[VOICE_MCP_TOKENS_FIELD]
        assert (await w.submit()).status_code == 200
        assert "voice_mcp_call" not in w.dump()
        assert await w.kit.voice.verify(tokens["facts"], MCPResource.FACTS) is None
        assert await w.kit.voice.verify(tokens["drive"], MCPResource.DRIVE) is None

    async def test_revocation_runs_with_the_knob_off(self):
        w = World()
        await w.prepare()
        tokens = (await (await w.session_config()).get_json())[VOICE_MCP_TOKENS_FIELD]
        off = World(minting=False, kit=w.kit)      # the same store and service, the knob turned off mid-call
        assert (await off.submit()).status_code == 200
        assert await w.kit.voice.verify(tokens["facts"], MCPResource.FACTS) is None

    async def test_a_failed_revoke_still_releases_the_one_call_marker(self, caplog):
        w = World()
        await w.setup.claim(USER, {"kind": "web"}, ttl_s=300)
        await w.prepare()
        await w.session_config()

        async def failing_delete(_ticket):
            raise RuntimeError("firestore down")
        w.kit.voice.revoke = failing_delete
        assert (await w.submit()).status_code == 200
        assert f"voice_one_call:{USER}" not in w.kit.docs
        assert any("voice MCP call record not deleted" in r.getMessage() for r in caplog.records)

    async def test_without_a_voice_service_submit_works_as_before(self):
        w = World(voice_service=False)
        await w.prepare()
        assert (await w.submit()).status_code == 200


@pytest.mark.usefixtures("transactions")
async def test_no_log_record_from_mint_to_revoke_contains_a_token(caplog):
    caplog.set_level(logging.DEBUG)
    w = World()
    await w.prepare()
    tokens = (await (await w.session_config()).get_json())[VOICE_MCP_TOKENS_FIELD]
    await w.kit.voice.verify(tokens["facts"], MCPResource.FACTS)
    await w.kit.voice.verify(tokens["drive"], MCPResource.FACTS)
    await w.submit()
    await w.kit.voice.verify(tokens["drive"], MCPResource.DRIVE)
    text = "\n".join(r.getMessage() for r in caplog.records)
    assert "[VoiceMCP] tokens minted" in text and "[VoiceMCP] call record deleted" in text
    for token in tokens.values():
        assert token not in text and token.split(".")[1] not in text and token.split(".")[2] not in text
    assert TICKET not in text
