"""prepare carries the call's voice mode to the session config and onto the stored records."""
import pytest

from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.services.voice_call_setup_service import VoiceCallSetupService
from tests.support.voice_tickets import DIGEST_KEY, call_kind_key, ticket_key
from tests.unit.web.test_voice_web_call_app import MemoryStore


class RecordingAgent:
    def __init__(self, error=None):
        self.modes, self.error = [], error

    async def session_config(self, user_id, account_id, mode=VoiceMode.LELIK):
        self.modes.append(mode)
        if self.error:
            raise self.error
        return {"instructions": "x", "tools": []}


class Alerts:
    def __init__(self):
        self.posts = []

    async def post(self, text):
        self.posts.append(text)


def _service(agent):
    store, alerts = MemoryStore(), Alerts()

    async def provider(user_id):
        return agent
    return VoiceCallSetupService(store, alerts, provider, digest_key=DIGEST_KEY), store, alerts


@pytest.mark.asyncio
async def test_mode_reaches_the_agent_the_ticket_and_the_call_kind_record():
    agent = RecordingAgent()
    service, store, _ = _service(agent)

    await service.prepare("t1", "u1", "a1", "web", VoiceMode.INTERPRETER)

    assert agent.modes == [VoiceMode.INTERPRETER]
    assert store.data[ticket_key("t1")]["mode"] == "interpreter"
    assert store.data[call_kind_key("t1")] == {"call_kind": "web", "mode": "interpreter"}


@pytest.mark.asyncio
async def test_mode_defaults_to_lelik():
    agent = RecordingAgent()
    service, store, _ = _service(agent)

    await service.prepare("t1", "u1", "a1", "phone")

    assert agent.modes == [VoiceMode.LELIK]
    assert store.data[call_kind_key("t1")]["mode"] == "lelik"


@pytest.mark.asyncio
async def test_unsupported_mode_releases_without_an_alert_and_propagates():
    service, store, alerts = _service(RecordingAgent(error=UnsupportedVoiceMode("no")))
    await service.claim("u1", {"call_id": "c1"}, ttl_s=300)

    with pytest.raises(UnsupportedVoiceMode):
        await service.prepare("t1", "u1", "a1", "web", VoiceMode.TUTOR)

    assert "voice_one_call:u1" not in store.data
    assert ticket_key("t1") not in store.data
    assert alerts.posts == []
