"""The one-call marker names the call that holds it, and a call releases it only while it is still its own
(VOICE_CALL_CONTROL_CHANNEL_RFC §7.5.4): a phone dial takes the marker over from a replaced web call, whose late release
must leave it alone. A marker without an owner (written before the field existed) is released as before."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from src.domain.voice_ticket_keys import one_call_owner
from src.services.voice_call_setup_service import VoiceCallSetupService, release_one_call_marker
from tests.support.voice_tickets import DIGEST_KEY, ticket_key

MARKER = "voice_one_call:u1"


class MemoryStore:
    def __init__(self, fails_on_get: bool = False):
        self.data = {}
        self.fails_on_get = fails_on_get

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        if self.fails_on_get:
            raise RuntimeError("firestore unavailable")
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


def _service(store):
    return VoiceCallSetupService(store, AsyncMock(), AsyncMock(), digest_key=DIGEST_KEY)


@pytest.mark.asyncio
async def test_a_call_releases_its_own_marker():
    store = MemoryStore()
    store.data[MARKER] = {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, "t-web")}

    assert await release_one_call_marker(store, DIGEST_KEY, "u1", "t-web") is True
    assert MARKER not in store.data


@pytest.mark.asyncio
async def test_a_call_leaves_a_marker_another_call_took_over():
    store = MemoryStore()
    taken_over = {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, "t-phone")}
    store.data[MARKER] = dict(taken_over)

    assert await release_one_call_marker(store, DIGEST_KEY, "u1", "t-web") is False
    assert store.data[MARKER] == taken_over


@pytest.mark.asyncio
@pytest.mark.parametrize("marker", [{"in_flight": True}, {"in_flight": True, "call_id": "web-call-0001"}],
                         ids=["phone-before-owner", "web-before-owner"])
async def test_a_marker_without_an_owner_is_released_by_any_call_of_the_user(marker):
    store = MemoryStore()
    store.data[MARKER] = marker

    assert await release_one_call_marker(store, DIGEST_KEY, "u1", "t-any") is True
    assert MARKER not in store.data


@pytest.mark.asyncio
async def test_an_absent_marker_is_a_no_op_release():
    store = MemoryStore()
    assert await release_one_call_marker(store, DIGEST_KEY, "u1", "t-any") is True
    assert store.data == {}


@pytest.mark.asyncio
async def test_a_marker_that_cannot_be_read_is_released_all_the_same():
    """A stuck marker locks the user out of calling; a lost one only loosens the one-call limit."""
    store = MemoryStore(fails_on_get=True)
    store.data[MARKER] = {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, "t-phone")}

    assert await release_one_call_marker(store, DIGEST_KEY, "u1", "t-web") is True
    assert MARKER not in store.data


@pytest.mark.asyncio
async def test_the_setup_release_deletes_the_ticket_but_not_a_marker_taken_over():
    store = MemoryStore()
    taken_over = {"in_flight": True, "owner": one_call_owner(DIGEST_KEY, "t-phone")}
    store.data[MARKER] = dict(taken_over)
    store.data[ticket_key("t-web")] = {"user_id": "u1"}

    await _service(store).release("t-web", "u1")

    assert ticket_key("t-web") not in store.data
    assert store.data[MARKER] == taken_over


@pytest.mark.asyncio
async def test_a_claim_with_owner_of_writes_the_owner_into_the_marker():
    store = MemoryStore()
    service = _service(store)

    assert await service.claim("u1", {"call_id": "c1", **service.owner_of("t1")}, ttl_s=300) is True
    assert store.data[MARKER] == {"in_flight": True, "call_id": "c1", "owner": one_call_owner(DIGEST_KEY, "t1")}


@pytest.mark.asyncio
async def test_wait_released_returns_once_the_marker_is_gone():
    store = MemoryStore()
    store.data[MARKER] = {"in_flight": True}

    async def release_soon():
        await asyncio.sleep(0.03)
        del store.data[MARKER]

    releaser = asyncio.ensure_future(release_soon())
    assert await _service(store).wait_released("u1", wait_s=2.0, poll_s=0.01) is True
    assert releaser.done()


@pytest.mark.asyncio
async def test_wait_released_gives_up_at_the_bound():
    store = MemoryStore()
    store.data[MARKER] = {"in_flight": True}

    assert await asyncio.wait_for(_service(store).wait_released("u1", wait_s=0.05, poll_s=0.01), timeout=2) is False
    assert MARKER in store.data
