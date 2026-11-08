"""session-config: the one-call marker earns its full-call TTL only when the response succeeds.

A failed token mint answers 500; the relay then sends no end-of-call report and the ticket is already consumed, so
nothing would release a marker extended before the mint (an hour of "busy" line). The marker must keep its short
setup-window TTL on a failed response, and be extended on success.
"""
from typing import Any, Dict, Optional, Tuple
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint

SETUP_TTL_S = 300
CALL_TTL_S = 3600
MARKER_KEY = "voice_one_call:user-1"
DIGEST_KEY = b"k" * 32


class TtlStore:
    """Ephemeral store with real semantics: a key holds (value, ttl); `set` replaces both."""

    def __init__(self, docs: Dict[str, Tuple[Any, int]]) -> None:
        self.docs = dict(docs)

    async def get(self, key: str) -> Optional[Any]:
        entry = self.docs.get(key)
        return entry[0] if entry else None

    async def get_and_delete(self, key: str) -> Optional[Any]:
        entry = self.docs.pop(key, None)
        return entry[0] if entry else None

    async def set(self, key: str, value: Any, ttl_s: int) -> None:
        self.docs[key] = (value, ttl_s)


class FakeMinter:
    def __init__(self, *, raises: bool = False) -> None:
        self.raises = raises

    async def mint(self, ticket: str, user_id: str, account_id: str):
        if self.raises:
            raise RuntimeError("store down")
        return None   # the user is no longer allowed: warned by the real service, not raised


def _world(minter: FakeMinter):
    from src.web.voice_control_plane_app import ticket_key
    config = {"instructions": "you are Lelik", "user_id": "user-1", "account_id": "acc-1",
              "mode": "lelik", "voice": {"provider": "xai"}}
    store = TtlStore({
        ticket_key(DIGEST_KEY, "t1"): (config, 300),
        MARKER_KEY: ({"in_flight": True, "call_id": "c1"}, SETUP_TTL_S),
    })
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=DIGEST_KEY, one_call_ttl_s=CALL_TTL_S, voice_mcp_tokens=minter, voice_mcp_minting=True))
    return store, app.test_client()


async def _redeem(client):
    return await client.post("/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"})


@pytest.mark.asyncio
async def test_a_failed_token_mint_answers_500_and_leaves_the_marker_its_short_ttl():
    store, client = _world(FakeMinter(raises=True))

    response = await _redeem(client)

    assert response.status_code == 500
    assert store.docs[MARKER_KEY][1] == SETUP_TTL_S


@pytest.mark.asyncio
async def test_a_mint_that_returns_no_tokens_still_succeeds_and_extends_the_marker():
    store, client = _world(FakeMinter())

    response = await _redeem(client)

    assert response.status_code == 200
    assert store.docs[MARKER_KEY][1] == CALL_TTL_S
