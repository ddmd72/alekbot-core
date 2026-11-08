"""The resume record: handle + mode + provider per user, consumed on use, expiring with the store's TTL."""
import pytest

from src.services.voice_resume_service import VoiceResumeService


class _Store:
    def __init__(self):
        self.data, self.ttls = {}, {}

    async def set(self, key, value, ttl_s):
        self.data[key], self.ttls[key] = value, ttl_s

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


@pytest.mark.asyncio
async def test_keep_stores_only_handle_mode_and_provider_for_25_minutes():
    store = _Store()

    await VoiceResumeService(store).keep("usr-1", "conv-0001-invented", "lelik", "xai")

    assert store.data == {"voice_resume:usr-1": {"handle": "conv-0001-invented", "mode": "lelik", "provider": "xai"}}
    assert store.ttls == {"voice_resume:usr-1": 25 * 60}


@pytest.mark.asyncio
async def test_take_returns_the_handle_and_consumes_it():
    store = _Store()
    service = VoiceResumeService(store)
    await service.keep("usr-1", "conv-0001-invented", "lelik", "xai")

    assert await service.take("usr-1", "lelik", "xai") == "conv-0001-invented"
    assert await service.take("usr-1", "lelik", "xai") is None
    assert store.data == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, provider", [("interpreter", "xai"), ("lelik", "openai")])
async def test_take_refuses_another_mode_or_provider_and_keeps_the_record(mode, provider):
    store = _Store()
    service = VoiceResumeService(store)
    await service.keep("usr-1", "conv-0001-invented", "lelik", "xai")

    assert await service.take("usr-1", mode, provider) is None
    assert "voice_resume:usr-1" in store.data


@pytest.mark.asyncio
async def test_take_for_another_user_finds_nothing():
    store = _Store()
    service = VoiceResumeService(store)
    await service.keep("usr-1", "conv-0001-invented", "lelik", "xai")

    assert await service.take("usr-2", "lelik", "xai") is None


@pytest.mark.asyncio
async def test_an_expired_record_is_absent():
    class _Expired(_Store):
        async def get(self, key):  # the store's contract: an expired value reads as absent
            return None

    assert await VoiceResumeService(_Expired()).take("usr-1", "lelik", "xai") is None
