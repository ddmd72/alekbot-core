"""session-config: redeeming a ticket extends the one-call marker to the full call TTL.

Fix round 1, Finding 2 / Ruling 4: `VoiceWebCallApp.start()` no longer extends the one-call
marker past the short setup-window TTL (see test_voice_web_call_app_failures.py). The marker is
extended here instead — the relay's `/voice/session-config` redemption is the point where a call
is actually confirmed live, not merely requested. A failure while extending the marker must never
turn a successful ticket redemption into a failed response for the relay.
"""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


def _app(ephemeral_store, one_call_ttl_s=None):
    quota_service = AsyncMock()
    prompt_content_store = AsyncMock()
    summary_consumer = AsyncMock()
    oidc_verifier = AsyncMock(return_value=True)
    alert_sink = AsyncMock()

    kwargs = dict(
        ephemeral_store=ephemeral_store,
        quota_service=quota_service,
        prompt_content_store=prompt_content_store,
        summary_consumer=summary_consumer,
        oidc_verifier=oidc_verifier,
        alert_sink=alert_sink,
    )
    if one_call_ttl_s is not None:
        kwargs["one_call_ttl_s"] = one_call_ttl_s

    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(**kwargs))
    return app


@pytest.mark.asyncio
async def test_redeeming_the_ticket_extends_the_marker_to_the_full_call_ttl():
    ephemeral_store = AsyncMock()
    ephemeral_store.get_and_delete.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1",
    }
    existing_marker = {"in_flight": True, "call_id": "c1"}
    ephemeral_store.get.return_value = existing_marker

    client = _app(ephemeral_store).test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    ephemeral_store.get.assert_awaited_once_with("voice_one_call:u1")
    ephemeral_store.set.assert_awaited_once_with("voice_one_call:u1", existing_marker, ttl_s=3600)


@pytest.mark.asyncio
async def test_extension_respects_a_custom_one_call_ttl_s():
    ephemeral_store = AsyncMock()
    ephemeral_store.get_and_delete.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1",
    }
    existing_marker = {"in_flight": True, "call_id": "c1"}
    ephemeral_store.get.return_value = existing_marker

    client = _app(ephemeral_store, one_call_ttl_s=120).test_client()
    await client.post("/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"})

    ephemeral_store.set.assert_awaited_once_with("voice_one_call:u1", existing_marker, ttl_s=120)


@pytest.mark.asyncio
async def test_no_marker_to_extend_is_a_silent_noop():
    ephemeral_store = AsyncMock()
    ephemeral_store.get_and_delete.return_value = {
        "instructions": "you are Lelik", "user_id": "u1", "account_id": "a1",
    }
    ephemeral_store.get.return_value = None

    client = _app(ephemeral_store).test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    ephemeral_store.set.assert_not_called()


@pytest.mark.asyncio
async def test_a_store_error_during_extension_still_returns_the_config():
    ephemeral_store = AsyncMock()
    config = {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1"}
    ephemeral_store.get_and_delete.return_value = config
    ephemeral_store.get.side_effect = RuntimeError("store unavailable")

    client = _app(ephemeral_store).test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    body = await response.get_json()
    assert body == config


@pytest.mark.asyncio
async def test_a_store_error_on_the_set_call_still_returns_the_config():
    ephemeral_store = AsyncMock()
    config = {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1"}
    ephemeral_store.get_and_delete.return_value = config
    ephemeral_store.get.return_value = {"in_flight": True, "call_id": "c1"}
    ephemeral_store.set.side_effect = RuntimeError("store unavailable")

    client = _app(ephemeral_store).test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    body = await response.get_json()
    assert body == config
