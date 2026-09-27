from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.voice_call_setup_service import VoiceCallSetupError, VoiceCallSetupService


def _service(store=None, agent=None):
    store = store or AsyncMock()
    alert = AsyncMock()
    provider = AsyncMock(return_value=agent)
    return VoiceCallSetupService(store, alert, provider), store, alert


@pytest.mark.asyncio
async def test_claim_refuses_when_a_call_is_in_flight():
    store = AsyncMock()
    store.get.return_value = {"in_flight": True}
    service, _, _ = _service(store)
    assert await service.claim("u1", {"call_id": "c1"}, ttl_s=300) is False
    store.set.assert_not_called()


@pytest.mark.asyncio
async def test_claim_writes_the_holder_into_the_marker():
    store = AsyncMock()
    store.get.return_value = None
    service, _, _ = _service(store)
    assert await service.claim("u1", {"call_id": "c1"}, ttl_s=300) is True
    store.set.assert_awaited_once_with("voice_one_call:u1", {"in_flight": True, "call_id": "c1"}, ttl_s=300)


@pytest.mark.asyncio
async def test_prepare_stores_session_identity_and_kind_on_the_ticket():
    agent = MagicMock()
    agent.session_config = AsyncMock(return_value={"instructions": "you are Lelik", "tools": []})
    service, store, _ = _service(agent=agent)
    await service.prepare("t1", "u1", "a1", "web")
    writes = {c.args[0]: c.args[1] for c in store.set.await_args_list}
    assert writes["voice_ticket:t1"] == {"instructions": "you are Lelik", "tools": [],
                                          "user_id": "u1", "account_id": "a1", "call_kind": "web"}
    assert writes["voice_call_kind:t1"] == {"call_kind": "web"}


@pytest.mark.asyncio
async def test_prepare_failure_releases_alerts_and_raises():
    agent = MagicMock()
    agent.session_config = AsyncMock(side_effect=RuntimeError("no prompt"))
    service, store, alert = _service(agent=agent)
    with pytest.raises(VoiceCallSetupError):
        await service.prepare("t1", "u1", "a1", "web")
    deleted = {c.args[0] for c in store.delete.await_args_list}
    assert deleted == {"voice_ticket:t1", "voice_one_call:u1"}
    alert.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_prepare_without_a_configured_lelik_fails_closed():
    service, store, alert = _service(agent=None)
    with pytest.raises(VoiceCallSetupError):
        await service.prepare("t1", "u1", "a1", "web")
    alert.post.assert_awaited_once()
