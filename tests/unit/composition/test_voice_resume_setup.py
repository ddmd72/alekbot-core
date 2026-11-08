"""build_voice_resume_service returns a service that works over the store it is given."""
import pytest

from src.composition.voice_resume_setup import build_voice_resume_service


class _Store:
    def __init__(self):
        self.data = {}

    async def set(self, key, value, ttl_s):
        self.data[key] = value

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


@pytest.mark.asyncio
async def test_the_built_service_keeps_and_hands_over_a_record_in_the_given_store():
    store = _Store()
    service = build_voice_resume_service(store=store)

    await service.keep("usr-1", "conv-0001-invented", "lelik", "xai")
    assert "voice_resume:usr-1" in store.data

    assert await service.take("usr-1", "lelik", "xai") == "conv-0001-invented"
    assert store.data == {}
