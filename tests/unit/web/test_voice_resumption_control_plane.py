"""Resumption on the main service: /voice/submit-transcript keeps the handle (never after a button hangup),
/voice/session-config hands a matching one to the next call and consumes it."""
from unittest.mock import AsyncMock

import pytest
from quart import Quart

from src.services.voice_resume_service import VoiceResumeService
from src.web.voice_control_plane_app import create_voice_control_plane_blueprint
from tests.support.voice_tickets import DIGEST_KEY, call_kind_key, ticket_key
from src.domain.voice_ticket_keys import button_end_key

AUTH = {"Authorization": "Bearer x"}
HANDLE = "conv-0001-invented"


class _Store:
    def __init__(self, data=None):
        self.data, self.ttls = dict(data or {}), {}

    async def set(self, key, value, ttl_s):
        self.data[key], self.ttls[key] = value, ttl_s

    async def get(self, key):
        return self.data.get(key)

    async def delete(self, key):
        self.data.pop(key, None)

    async def get_and_delete(self, key):
        return self.data.pop(key, None)


def _app(store, with_resume=True):
    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=store, quota_service=AsyncMock(), prompt_content_store=AsyncMock(),
        summary_consumer=AsyncMock(), oidc_verifier=AsyncMock(return_value=True), alert_sink=AsyncMock(),
        digest_key=DIGEST_KEY, resume_service=VoiceResumeService(store) if with_resume else None))
    return app


async def _submit(app, **extra):
    body = {"call_id": "t1", "user_id": "usr-1", "account_id": "acc-1", "transcript_text": "", "usage_by_model": {},
            "turns": [], **extra}
    return await app.test_client().post("/voice/submit-transcript", json=body, headers=AUTH)


def _store_with_kind(mode="lelik", **extra):
    return _Store({call_kind_key("t1"): {"call_kind": "web", "mode": mode}, **extra})


@pytest.mark.asyncio
async def test_a_finished_call_keeps_its_handle_with_mode_and_provider():
    store = _store_with_kind("interpreter")

    await _submit(_app(store), resume_handle=HANDLE, resume_provider="xai")

    assert store.data["voice_resume:usr-1"] == {"handle": HANDLE, "mode": "interpreter", "provider": "xai"}
    assert store.ttls["voice_resume:usr-1"] == 25 * 60


@pytest.mark.asyncio
async def test_a_button_ended_call_stores_no_handle():
    store = _store_with_kind(**{button_end_key(DIGEST_KEY, "t1"): {"button": True}})

    response = await _submit(_app(store), resume_handle=HANDLE, resume_provider="xai")

    assert response.status_code == 200
    assert "voice_resume:usr-1" not in store.data


@pytest.mark.asyncio
async def test_no_handle_in_the_report_stores_nothing():
    store = _store_with_kind()

    await _submit(_app(store))

    assert "voice_resume:usr-1" not in store.data


@pytest.mark.asyncio
async def test_a_provider_that_cannot_resume_stores_nothing():
    store = _store_with_kind()

    await _submit(_app(store), resume_handle=HANDLE, resume_provider="openai")

    assert "voice_resume:usr-1" not in store.data


@pytest.mark.asyncio
async def test_an_unknown_call_mode_stores_nothing():
    store = _Store()  # no call-kind record: the mode is a guess, so no handle is kept

    await _submit(_app(store), resume_handle=HANDLE, resume_provider="xai")

    assert "voice_resume:usr-1" not in store.data


@pytest.mark.asyncio
async def test_a_failing_resume_store_never_fails_the_report():
    store = _store_with_kind()
    store.set = AsyncMock(side_effect=RuntimeError("store down"))

    response = await _submit(_app(store), resume_handle=HANDLE, resume_provider="xai")

    assert response.status_code == 200


def _ticket_record(mode="lelik", provider="xai"):
    return {"instructions": "p", "user_id": "usr-1", "account_id": "acc-1", "mode": mode, "tools": [],
            "voice": {"provider": provider, "turn_ownership": "provider", "voice": "rex", "reasoning_effort": "low"}}


async def _redeem(app):
    response = await app.test_client().post("/voice/session-config", json={"ticket": "t1"}, headers=AUTH)
    assert response.status_code == 200
    return await response.get_json()


@pytest.mark.asyncio
async def test_session_config_hands_over_a_matching_handle_and_consumes_it():
    store = _Store({ticket_key("t1"): _ticket_record(),
                    "voice_resume:usr-1": {"handle": HANDLE, "mode": "lelik", "provider": "xai"}})

    config = await _redeem(_app(store))

    assert config["resume"] == {"handle": HANDLE}
    assert "voice_resume:usr-1" not in store.data


@pytest.mark.asyncio
@pytest.mark.parametrize("mode, provider", [("interpreter", "xai"), ("lelik", "openai")])
async def test_session_config_gives_no_handle_for_another_mode_or_provider(mode, provider):
    store = _Store({ticket_key("t1"): _ticket_record(mode, provider),
                    "voice_resume:usr-1": {"handle": HANDLE, "mode": "lelik", "provider": "xai"}})

    config = await _redeem(_app(store))

    assert "resume" not in config


@pytest.mark.asyncio
async def test_session_config_without_a_record_or_a_service_has_no_resume_field():
    assert "resume" not in await _redeem(_app(_Store({ticket_key("t1"): _ticket_record()})))
    held = _Store({ticket_key("t1"): _ticket_record(),
                   "voice_resume:usr-1": {"handle": HANDLE, "mode": "lelik", "provider": "xai"}})
    assert "resume" not in await _redeem(_app(held, with_resume=False))


@pytest.mark.asyncio
async def test_a_failing_resume_read_still_serves_the_call_fresh():
    store = _Store({ticket_key("t1"): _ticket_record()})
    original_get = store.get

    async def get(key):
        if key.startswith("voice_resume:"):
            raise RuntimeError("store down")
        return await original_get(key)

    store.get = get

    assert "resume" not in await _redeem(_app(store))
