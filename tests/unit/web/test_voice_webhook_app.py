import pytest
from unittest.mock import AsyncMock, MagicMock

from quart import Quart

from src.web.voice_webhook_app import create_voice_webhook_blueprint


def _persona_factory(instructions="you are Lelik.\n<!-- CACHE_BOUNDARY -->\ndynamic"):
    """`persona_service_factory(user_id)` -> an object with async build_instructions()."""
    persona = MagicMock()
    persona.build_instructions = AsyncMock(return_value=instructions)
    return AsyncMock(return_value=persona), persona


@pytest.fixture
def deps():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    alert_sink = AsyncMock()
    notification_service = AsyncMock()
    lelik_agent = AsyncMock()
    lelik_agent_factory = MagicMock(return_value=lelik_agent)
    return (
        user_repository,
        ephemeral_store,
        alert_sink,
        notification_service,
        lelik_agent,
        lelik_agent_factory,
    )


def _app(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, _, lelik_agent_factory = deps
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=alert_sink,
        notification_service=notification_service,
        lelik_agent_factory=lelik_agent_factory,
        answer_url="https://main.example.com/voice/answer",
        # FIX I3 (final whole-branch review): the Twilio-facing routes now verify
        # the X-Twilio-Signature header via an injected async callable, the same
        # shape voice_control_plane_app injects its OIDC verifier. Mechanical
        # adaptation to a new REQUIRED dependency; no assertion in this file changed.
        signature_verifier=AsyncMock(return_value=True),
    ))
    return app


@pytest.mark.asyncio
async def test_unbound_number_rejected_and_alerted_before_any_callback(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, _ = deps
    user_repository.get_user_by_platform_id.return_value = None

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    user_repository.get_user_by_platform_id.assert_awaited_once_with("phone", "+346001")
    alert_sink.post.assert_awaited_once()
    ephemeral_store.set.assert_not_called()
    lelik_agent.execute.assert_not_called()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_bound_number_mints_ticket_and_marker_then_originates_callback(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, lelik_agent_factory = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = None  # no existing one-call marker

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Hangup" in body
    assert "<Reject" not in body

    set_calls = {c.args[0]: c.args[1] for c in ephemeral_store.set.await_args_list}
    assert any(key.startswith("voice_ticket:") for key in set_calls)
    assert "voice_one_call:u1" in set_calls
    lelik_agent_factory.assert_called_once_with(user_id="u1", account_id="a1", to_number="+346001")
    lelik_agent.execute.assert_awaited_once()
    alert_sink.post.assert_not_called()

    notification_service.notify_raw.assert_awaited_once()
    _, notify_kwargs = notification_service.notify_raw.await_args
    assert notify_kwargs["user_id"] == "u1"
    assert notify_kwargs["account_id"] == "a1"
    assert isinstance(notify_kwargs["text"], str) and notify_kwargs["text"]


@pytest.mark.asyncio
async def test_origination_failure_releases_ticket_and_marker_and_alerts(deps):
    """If lelik_agent.execute() raises (network blip, agent error, ...), the
    ticket and one-call marker minted just before the call must not be left
    stuck for up to one_call_ttl_s - this is the mint-side counterpart to
    Task 7's submit_transcript release-on-failure guarantee (commit
    d1dd648). The handler must also alert and return graceful TwiML, not a
    bare 500."""
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, lelik_agent_factory = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = None  # no existing one-call marker
    lelik_agent.execute.side_effect = RuntimeError("origination boom")

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    # Response must be graceful TwiML, not a crash.
    assert response.status_code == 200
    body = (await response.get_data()).decode()
    assert "<Response" in body

    set_calls = {c.args[0]: c.args[1] for c in ephemeral_store.set.await_args_list}
    ticket_key = next(key for key in set_calls if key.startswith("voice_ticket:"))

    deleted_keys = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert ticket_key in deleted_keys
    assert "voice_one_call:u1" in deleted_keys

    alert_sink.post.assert_awaited_once()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_second_call_refused_while_one_already_in_flight(deps):
    user_repository, ephemeral_store, alert_sink, notification_service, lelik_agent, _ = deps
    user_repository.get_user_by_platform_id.return_value = MagicMock(user_id="u1", account_id="a1")
    ephemeral_store.get.return_value = {"in_flight": True}

    app = _app(deps)
    client = app.test_client()
    response = await client.post("/voice/auth", form={"From": "+346001"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    lelik_agent.execute.assert_not_called()
    notification_service.notify_raw.assert_not_called()


@pytest.mark.asyncio
async def test_answer_webhook_hangs_up_on_machine_without_opening_session():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    persona_factory, _ = _persona_factory()

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True),
        persona_service_factory=persona_factory,
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "machine_end_beep"})

    body = (await response.get_data()).decode()
    assert "<Hangup" in body
    assert "<Connect" not in body
    persona_factory.assert_not_called()


@pytest.mark.asyncio
async def test_answer_webhook_assembles_persona_and_streams_on_human_pickup():
    user_repository = AsyncMock()
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    persona_factory, persona = _persona_factory()

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=user_repository,
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True),
        persona_service_factory=persona_factory,
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "human"})

    body = (await response.get_data()).decode()
    assert "<Connect>" in body
    assert "wss://relay.example.com/" in body
    assert "t1" in body
    persona_factory.assert_awaited_once_with("u1")
    persona.build_instructions.assert_awaited_once_with(user_id="u1", account_id="a1")
    stashed = ephemeral_store.set.await_args.args
    assert stashed[0] == "voice_ticket:t1"
    assert stashed[1]["instructions"] == "you are Lelik.\n<!-- CACHE_BOUNDARY -->\ndynamic"


@pytest.mark.asyncio
async def test_answer_webhook_rejects_when_ticket_not_found():
    """A redeemed/expired/forged ticket has no identity behind it in the
    EphemeralStore - reject the call rather than opening a session with no
    known user_id/account_id."""
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = None
    persona_factory, _ = _persona_factory()

    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=AsyncMock(return_value=True),
        persona_service_factory=persona_factory,
        relay_stream_url="wss://relay.example.com/",
    ))
    client = app.test_client()

    response = await client.post("/voice/answer?ticket=unknown", form={"CallSid": "CA1", "AnsweredBy": "human"})

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    persona_factory.assert_not_called()


# =============================================================================
# Final whole-branch review — FIX C1 / I1 / I3
# =============================================================================


def _signed_app(*, ephemeral_store, signature_verifier=None):
    """Blueprint wired the way main.py wires it, for the Twilio-facing seams."""
    app = Quart(__name__)
    app.register_blueprint(create_voice_webhook_blueprint(
        user_repository=AsyncMock(),
        ephemeral_store=ephemeral_store,
        alert_sink=AsyncMock(),
        notification_service=AsyncMock(),
        lelik_agent_factory=MagicMock(),
        answer_url="https://main.example.com/voice/answer",
        signature_verifier=signature_verifier or AsyncMock(return_value=True),
        persona_service_factory=_persona_factory()[0],
        relay_stream_url="wss://relay.example.com/",
    ))
    return app


@pytest.mark.asyncio
async def test_answer_reads_ticket_from_query_string_as_lelik_agent_actually_sends_it():
    """FIX C1 — the producer/consumer seam neither side's tests crossed.

    `LelikAgent.execute` builds the callback URL as
    `f"{answer_url}?{urlencode({'ticket': ticket})}"`; Twilio POSTs its own
    fields (CallSid, AnsweredBy, ...) in the BODY and leaves that query string
    untouched. Reading the ticket from `await request.form` therefore resolved
    an empty ticket on every real call and rejected all of them. This test
    constructs the request exactly as Twilio delivers it: ticket in the query
    string ONLY, never in the form body.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/answer?ticket=real-ticket",
        form={"CallSid": "CA1", "AnsweredBy": "human", "From": "+346001"},
    )

    body = (await response.get_data()).decode()
    assert "<Connect>" in body
    ephemeral_store.get.assert_awaited_once_with("voice_ticket:real-ticket")


@pytest.mark.asyncio
async def test_answer_does_not_read_the_ticket_from_the_form_body():
    """The other half of FIX C1's seam: a ticket that arrives ONLY in the POST
    body (the shape the route used to read) must not resolve. This is what
    keeps a future edit from quietly reverting to `form.get("ticket")` — with
    both readers in place the bug would be invisible again."""
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = None

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/answer",
        form={"CallSid": "CA1", "AnsweredBy": "human", "ticket": "body-only"},
    )

    body = (await response.get_data()).decode()
    assert "<Reject" in body
    ephemeral_store.get.assert_awaited_once_with("voice_ticket:")


@pytest.mark.asyncio
async def test_machine_detection_releases_ticket_and_one_call_marker():
    """FIX I1(a) — voicemail ends the call, so it must end the one-call window.

    The AMD branch used to hang up without deleting either key, stranding the
    caller behind `voice_one_call:{user_id}` for up to `one_call_ttl_s`
    (default 3600s) with no way to know why. Same two keys, same release
    pattern as the persona-assembly failure handler.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/answer?ticket=t1", form={"CallSid": "CA1", "AnsweredBy": "machine_end_beep"},
    )

    body = (await response.get_data()).decode()
    assert "<Hangup" in body
    assert "<Connect" not in body

    deleted = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert deleted == {"voice_ticket:t1", "voice_one_call:u1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("call_status", ["completed", "no-answer", "busy", "failed", "canceled"])
async def test_status_callback_releases_both_keys_on_terminal_status(call_status):
    """FIX I1(b) — `/voice/status` did not exist at all.

    `TwilioTelephonyAdapter` subscribes to the status callbacks and
    `_build_lelik` points at this URL, so every one of them 404'd. The case
    this route exists for is a callback that is dialed but never answered
    (rings out / busy / carrier failure): it reaches neither `/voice/answer`
    nor `/voice/submit-transcript`, so nothing else can release the marker.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/status?ticket=t1", form={"CallSid": "CA1", "CallStatus": call_status},
    )

    # Twilio does not parse TwiML from a status callback — a bare 200 is the contract.
    assert response.status_code == 200
    deleted = {c.args[0] for c in ephemeral_store.delete.await_args_list}
    assert deleted == {"voice_ticket:t1", "voice_one_call:u1"}


@pytest.mark.asyncio
@pytest.mark.parametrize("call_status", ["initiated", "ringing", "in-progress"])
async def test_status_callback_ignores_non_terminal_statuses(call_status):
    """Releasing on `ringing` would free the one-call marker while the call is
    still live, which is the opposite of what the marker is for."""
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/status?ticket=t1", form={"CallSid": "CA1", "CallStatus": call_status},
    )

    assert response.status_code == 200
    ephemeral_store.delete.assert_not_called()


@pytest.mark.asyncio
async def test_status_callback_is_idempotent_when_ticket_already_released():
    """`/voice/answer` (AMD), the persona-failure handler and
    `/voice/session-config` all consume the ticket too, and Twilio retries
    callbacks — arriving after the release must be a no-op, not a crash."""
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = None

    client = _signed_app(ephemeral_store=ephemeral_store).test_client()
    response = await client.post(
        "/voice/status?ticket=t1", form={"CallSid": "CA1", "CallStatus": "completed"},
    )

    assert response.status_code == 200
    ephemeral_store.delete.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path,form",
    [
        ("/voice/auth", {"From": "+346001"}),
        ("/voice/answer?ticket=t1", {"CallSid": "CA1", "AnsweredBy": "human"}),
        ("/voice/status?ticket=t1", {"CallSid": "CA1", "CallStatus": "completed"}),
    ],
)
async def test_every_twilio_route_rejects_an_invalid_signature(path, form):
    """FIX I3 — all three routes accepted any POST from anyone before this.

    Every other external entry point in the repo authenticates its caller
    (Slack signature, Telegram webhook secret, /worker OIDC); these did not.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}

    client = _signed_app(
        ephemeral_store=ephemeral_store,
        signature_verifier=AsyncMock(return_value=False),
    ).test_client()
    response = await client.post(path, form=form)

    assert response.status_code == 403
    # Rejected before any state was read or written.
    ephemeral_store.get.assert_not_called()
    ephemeral_store.set.assert_not_called()
    ephemeral_store.delete.assert_not_called()


@pytest.mark.asyncio
async def test_signature_verifier_receives_the_full_external_url_including_query_string():
    """Twilio's HMAC covers the URL it POSTed to, and the call ticket lives in
    that query string — so the verifier must be handed the external `https://`
    URL with the query intact, not Cloud Run's internal `http://` view of it.
    Hypercorn here trusts no forwarded headers (main.py builds a bare
    `HypercornConfig`), so the scheme has to come from `X-Forwarded-Proto`.
    """
    ephemeral_store = AsyncMock()
    ephemeral_store.get.return_value = {"user_id": "u1", "account_id": "a1"}
    verifier = AsyncMock(return_value=True)

    client = _signed_app(
        ephemeral_store=ephemeral_store, signature_verifier=verifier,
    ).test_client()
    await client.post(
        "/voice/answer?ticket=t1",
        form={"CallSid": "CA1", "AnsweredBy": "human"},
        headers={
            "X-Twilio-Signature": "sig-abc",
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "main.example.com",
        },
    )

    url, form_params, signature = verifier.await_args.args
    assert url == "https://main.example.com/voice/answer?ticket=t1"
    assert form_params == {"CallSid": "CA1", "AnsweredBy": "human"}
    assert signature == "sig-abc"
