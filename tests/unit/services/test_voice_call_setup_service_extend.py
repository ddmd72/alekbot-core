"""VoiceCallSetupService.claim_extend — re-writes the marker with a longer TTL once the call goes live."""
from unittest.mock import AsyncMock

import pytest

from src.services.voice_call_setup_service import VoiceCallSetupService


@pytest.mark.asyncio
async def test_claim_extend_rewrites_the_marker_with_the_new_ttl():
    store = AsyncMock()
    await VoiceCallSetupService(store, AsyncMock(), AsyncMock()).claim_extend("u1", {"call_id": "c1"}, ttl_s=3600)
    store.set.assert_awaited_once_with("voice_one_call:u1", {"in_flight": True, "call_id": "c1"}, ttl_s=3600)
