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
    # FIX C3 (final whole-branch review): a summary-pipeline failure is now
    # alerted, not only logged. Mechanical adaptation to a new required
    # dependency; no assertion below changed because of it.
    alert_sink = AsyncMock()

    app = Quart(__name__)
    app.register_blueprint(create_voice_control_plane_blueprint(
        ephemeral_store=ephemeral_store,
        quota_service=quota_service,
        prompt_content_store=prompt_content_store,
        summary_consumer=summary_consumer,
        oidc_verifier=oidc_verifier,
        alert_sink=alert_sink,
    ))
    return app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, oidc_verifier, alert_sink


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
    app, _es, _qs, _pcs, _sc, oidc_verifier, _alert = app_and_deps
    oidc_verifier.return_value = False

    client = app.test_client()
    response = await client.post("/voice/session-config", json={"ticket": "t"}, headers={"Authorization": "Bearer bad"})

    assert response.status_code == 401


@pytest.mark.asyncio
async def test_submit_transcript_records_usage_and_calls_summary_consumer(app_and_deps):
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

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
async def test_submit_transcript_releases_marker_even_if_usage_recording_fails(app_and_deps):
    """A `quota_service.record_usage` failure (e.g. a future QuotaService
    implementation that doesn't swallow its own errors, or a malformed
    usage_by_model payload) must not leave the one-call marker stuck either —
    the guarantee covers the whole post-authentication body, not just the
    summary_consumer call."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps
    quota_service.record_usage.side_effect = RuntimeError("boom")

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
    ephemeral_store.delete.assert_awaited_once_with("voice_one_call:u1")
    summary_consumer.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_transcript_passes_user_and_account_id_to_summary_consumer(app_and_deps):
    """Task 15: the real summary_consumer (CompanionExtractorRunner.extract +
    notify_call_summary) needs user_id/account_id to identify who to summarize
    for and where to deliver — both must reach the consumer as kwargs."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi there",
        "usage_by_model": {},
        "turns": [{"request_text": "hi", "response_text": "hello"}],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    summary_consumer.assert_awaited_once_with(
        call_id="c1",
        user_id="u1",
        account_id="a1",
        transcript_text="hi there",
        turns=[{"request_text": "hi", "response_text": "hello"}],
    )


@pytest.mark.asyncio
async def test_submit_transcript_releases_marker_even_if_summary_consumer_fails(app_and_deps):
    """A summary-consumer failure must not leave the one-call marker stuck —
    that would permanently lock the user out of ever calling again."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps
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


@pytest.mark.asyncio
async def test_submit_transcript_records_each_turn_to_prompt_content_store(app_and_deps):
    """RFC §7 item 10 / plan self-review: the realtime call's actual content
    (not just its cost) must reach PromptContentStore, one record_turn call
    per VoiceTurnSegment."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi there",
        "usage_by_model": {"gpt-realtime-2.1": {"audio_input_tokens": 100}},
        "turns": [
            {
                "request_text": "what's the weather",
                "response_text": "sunny in Valencia",
                "started_at": "2026-09-22T10:00:00+00:00",
                "ended_at": "2026-09-22T10:00:02+00:00",
                "finish_reason": "completed",
            },
            {
                "request_text": "thanks",
                "response_text": "you're welcome",
                "started_at": "2026-09-22T10:00:05+00:00",
                "ended_at": "2026-09-22T10:00:06+00:00",
                "finish_reason": "completed",
            },
        ],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    assert prompt_content_store.record_turn.await_count == 2

    first_call = prompt_content_store.record_turn.await_args_list[0]
    assert first_call.kwargs["agent_id"] == "lelik_agent_u1"
    assert first_call.kwargs["agent_type"] == "lelik"
    assert first_call.kwargs["account_id"] == "a1"
    assert first_call.kwargs["turn"] == 0
    assert first_call.kwargs["provider"] == "openai"
    assert first_call.kwargs["request"].model_name == "gpt-realtime-2.1"
    assert "what's the weather" in first_call.kwargs["request"].messages[0].parts[0].text
    assert first_call.kwargs["response"].text == "sunny in Valencia"
    assert first_call.kwargs["latency_ms"] == pytest.approx(2000.0)

    second_call = prompt_content_store.record_turn.await_args_list[1]
    assert second_call.kwargs["turn"] == 1
    assert second_call.kwargs["latency_ms"] == pytest.approx(1000.0)


@pytest.mark.asyncio
async def test_submit_transcript_skips_recording_when_no_turns(app_and_deps):
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "",
        "usage_by_model": {},
        "turns": [],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    prompt_content_store.record_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_submit_transcript_releases_marker_even_if_turn_recording_raises(app_and_deps):
    """A malformed turn (e.g. bad ISO timestamp) must not break the marker
    release or the rest of the handler — record_turn recording is
    best-effort, matching the existing usage-recording and summary_consumer
    error-isolation pattern in this same function."""
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

    client = app.test_client()
    payload = {
        "call_id": "c1",
        "user_id": "u1",
        "account_id": "a1",
        "transcript_text": "hi",
        "usage_by_model": {},
        "turns": [
            {
                "request_text": "hi",
                "response_text": "hello",
                "started_at": "not-a-timestamp",
                "ended_at": "also-not-a-timestamp",
                "finish_reason": "completed",
            }
        ],
    }
    response = await client.post("/voice/submit-transcript", json=payload, headers={"Authorization": "Bearer x"})

    assert response.status_code == 200
    ephemeral_store.delete.assert_awaited_once_with("voice_one_call:u1")
    summary_consumer.assert_awaited_once()


# =============================================================================
# Final whole-branch review — FIX I2 / C3
# =============================================================================


@pytest.mark.asyncio
async def test_session_config_consumes_the_ticket_so_it_cannot_be_replayed(app_and_deps):
    """FIX I2 — the ticket was a replayable bearer credential.

    `/voice/session-config` resolved it but never deleted it, so for the
    ticket's whole TTL (`voice_webhook_app._TICKET_TTL_S`, 300s) anyone who
    captured it could redeem it again and read back the owner's assembled
    persona: biographical facts, location, preferences and standing
    directives. The relay fetches the config exactly once per call
    (`HttpCallControlPlaneAdapter.fetch_session_config`), so single use costs
    the legitimate caller nothing.
    """
    app, ephemeral_store, *_rest = app_and_deps
    ephemeral_store.get.return_value = {"instructions": "you are Lelik", "user_id": "u1", "account_id": "a1"}

    client = app.test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "t1"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 200
    ephemeral_store.delete.assert_awaited_once_with("voice_ticket:t1")


@pytest.mark.asyncio
async def test_session_config_does_not_consume_a_ticket_it_could_not_resolve(app_and_deps):
    """An unknown/expired ticket 404s without a delete — there is nothing to
    consume, and issuing a delete for an attacker-supplied key would turn this
    endpoint into an unauthenticated eviction primitive."""
    app, ephemeral_store, *_rest = app_and_deps
    ephemeral_store.get.return_value = None

    client = app.test_client()
    response = await client.post(
        "/voice/session-config", json={"ticket": "unknown"}, headers={"Authorization": "Bearer x"}
    )

    assert response.status_code == 404
    ephemeral_store.delete.assert_not_called()


@pytest.mark.asyncio
async def test_summary_consumer_failure_posts_an_ops_alert(app_and_deps):
    """FIX C3 — this `except` swallowed the whole end-of-call summary pipeline.

    A failure here is invisible from the outside: the call completes, the usage
    is billed, and simply nothing reaches chat or memory. That is exactly what
    the missing `lelik_summarizer` Firestore artefacts produced
    (`build_for_agent` fails closed -> AgentResponse.failure -> the runner
    raises -> here). The artefacts are fixed; the failure CLASS is permanent,
    so it now alerts as well as logs.
    """
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps
    summary_consumer.side_effect = RuntimeError("PromptBuilder failed: profile not found")

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
    alert_sink.post.assert_awaited_once()
    alert_text = alert_sink.post.await_args.args[0]
    assert "c1" in alert_text


@pytest.mark.asyncio
async def test_no_alert_when_the_summary_pipeline_succeeds(app_and_deps):
    app, ephemeral_store, quota_service, prompt_content_store, summary_consumer, _, alert_sink = app_and_deps

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
    alert_sink.post.assert_not_called()
