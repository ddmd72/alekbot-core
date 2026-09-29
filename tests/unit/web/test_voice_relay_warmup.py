"""POST /voice/relay-warmup — Cloud Scheduler keeps the voice relay warm through the main service.

The relay's cold start (up to ~67 s, 2026-09-29) outlasts Cloudflare's ~8 s WebSocket handshake,
and Scheduler cannot hit the relay directly (its Content-Length is rejected by the relay's
websockets parser).
"""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


def _app(relay_warmup=None, oidc_ok=True):
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=AsyncMock(), quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=oidc_ok),
        alert_sink=AsyncMock(), relay_warmup=relay_warmup,
    ))
    return app


@pytest.mark.asyncio
async def test_warmup_pings_the_relay_and_reports_its_status():
    ping = AsyncMock(return_value=200)

    resp = await _app(ping).test_client().post("/voice/relay-warmup", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert (await resp.get_json()) == {"warmed": True, "relay_status": 200}
    ping.assert_awaited_once()


@pytest.mark.asyncio
async def test_warmup_requires_the_scheduler_oidc_token():
    ping = AsyncMock(return_value=200)

    resp = await _app(ping, oidc_ok=False).test_client().post("/voice/relay-warmup")

    assert resp.status_code == 401
    ping.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failed_ping_is_a_502_not_a_crash():
    resp = await _app(AsyncMock(side_effect=TimeoutError("cold"))).test_client().post(
        "/voice/relay-warmup", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_without_a_relay_configured_it_is_a_no_op():
    resp = await _app(None).test_client().post("/voice/relay-warmup", headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert (await resp.get_json())["warmed"] is False
