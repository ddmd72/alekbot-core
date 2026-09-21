import pytest
from unittest.mock import AsyncMock

from quart import Quart

from src.web.voice_control_plane_app import create_voice_control_plane_blueprint


@pytest.fixture
def app_and_deps():
    ephemeral_store = AsyncMock()
    quota_service = AsyncMock()
    prompt_content_store = AsyncMock()
    summary_consumer = AsyncMock()
    oidc_verifier = AsyncMock(return_value=True)

    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=ephemeral_store,
        quota_service=quota_service,
        prompt_content_store=prompt_content_store,
        summary_consumer=summary_consumer,
        oidc_verifier=oidc_verifier,
    ))
    return app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, oidc_verifier


@pytest.mark.asyncio
async def test_session_config_resolves_ticket(app_and_deps):
    app, ephemeral_store, *_ = app_and_deps
    ephemeral_store.get.return_value = {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1"}

    client = app.test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "ticket-1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    body = await response.get_json()
    assert body == {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1"}
    ephemeral_store.get.assert_awaited_once_with("voice_ticket:ticket-1")


@pytest.mark.asyncio
async def test_session_config_404_on_unknown_ticket(app_and_deps):
    app, ephemeral_store, *_ = app_and_deps
    ephemeral_store.get.return_value = None

    client = app.test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "unknown"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 404


@pytest.mark.asyncio
async def test_session_config_401_when_oidc_verification_fails(app_and_deps):
    app, *_rest, oidc_verifier = app_and_deps
    oidc_verifier.return_value = False

    client = app.test_client()
    response = await client.post("/voice/session-config", json={"ticket": "t"}, headers={"Authorization": "Bearer bad"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_transcript_records_usage_and_calls_summary_consumer(app_and_deps):
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _ = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi there",
        "usage_by_model": {"gpt-realtime-2.1": {"audio_input_tokens": 100, "text_output_tokens": 10}},
        "turns": [],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    summary_consumer.assert_awaited_once()
    quota_service.record_usage.assert_awaited()


@pytest.mark.asyncio
async def test_submit_transcript_releases_one_call_marker(app_and_deps):
    """The auth webhook (a later task) writes a one-call-per-user marker at
    ``voice_one_call:{user_id}`` into this same ephemeral_store before dialing out.
    submit-transcript is the natural end-of-call hook, so it must release that
    marker — otherwise a completed call permanently locks the user out of
    ever calling again."""
    app, ephemeral_store, *_rest = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi there",
        "usage_by_model": {},
        "turns": [],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    ephemeral_store.delete.assert_awaited_once_with("voice_one_call:u1")


@pytest.mark.asyncio
async def test_submit_transcript_releases_marker_even_if_summary_consumer_fails(app_and_deps):
    """A summary-consumer failure must not leave the one-call marker stuck —
    that would permanently lock the user out of ever calling again."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _ = app_and_deps
    summary_consumer.side_effect = RuntimeError("boom")

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi there",
        "usage_by_model": {},
        "turns": [],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    ephemeral_store.delete.assert_awaited_once_with("voice_one_call:u1")
