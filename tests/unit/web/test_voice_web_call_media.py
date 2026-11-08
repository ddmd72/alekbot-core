"""`WebCallMedia`: the web call's record and media legs, shared by the web-call blueprint and the Twilio dial-in
(VOICE_CALL_CONTROL_CHANNEL_RFC §7.5)."""
from unittest.mock import AsyncMock

import pytest

from src.services.voice_call_setup_service import VoiceCallSetupService
from src.web.voice_web_call_media import WebCallMedia
from tests.support.aes_gcm_cipher import AesGcmTestCipher
from tests.support.voice_tickets import DIGEST_KEY, seal_ticket, ticket_key
from tests.unit.web.test_voice_phone_replaces_web_call import FakeMediaRoom, MemoryStore

CIPHER = AesGcmTestCipher()
RECORD_KEY = "voice_web_call:web-call-0001"


async def _world(*, redeemed: bool):
    store, room = MemoryStore(), FakeMediaRoom()
    setup = VoiceCallSetupService(store, AsyncMock(), AsyncMock(), digest_key=DIGEST_KEY)
    store.data[RECORD_KEY] = {"user_id": "usr-1", "adapter_ids": ["S1-in", "S1-eg"],
                              "ticket_ct": await seal_ticket(CIPHER, RECORD_KEY, "t-web")}
    store.data["voice_one_call:usr-1"] = {"in_flight": True, "call_id": "web-call-0001",
                                          **setup.owner_of("t-web")}
    if not redeemed:
        store.data[ticket_key("t-web")] = {"user_id": "usr-1"}
    return store, room, WebCallMedia(room, store, setup, CIPHER, DIGEST_KEY)


@pytest.mark.asyncio
async def test_the_record_is_read_only_for_its_owner():
    store, _, media = await _world(redeemed=True)
    assert await media.record("web-call-0001", "usr-1") == store.data[RECORD_KEY]
    assert await media.record("web-call-0001", "usr-2") is None
    assert await media.record("web-call-0002", "usr-1") is None


@pytest.mark.asyncio
async def test_the_ticket_opens_from_the_record():
    store, _, media = await _world(redeemed=True)
    assert await media.ticket("web-call-0001", store.data[RECORD_KEY]) == "t-web"


@pytest.mark.asyncio
async def test_closing_a_running_call_closes_its_legs_and_leaves_the_marker_to_the_relay():
    store, room, media = await _world(redeemed=True)

    await media.close(store.data[RECORD_KEY], "t-web", "usr-1")

    assert room.closed == [["S1-in", "S1-eg"]]
    assert "voice_one_call:usr-1" in store.data and RECORD_KEY in store.data


@pytest.mark.asyncio
async def test_closing_a_call_the_relay_never_started_releases_its_ticket_and_marker():
    store, room, media = await _world(redeemed=False)

    await media.close(store.data[RECORD_KEY], "t-web", "usr-1")

    assert ticket_key("t-web") not in store.data
    assert "voice_one_call:usr-1" not in store.data
