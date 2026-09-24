"""
Voice webhooks — Twilio's entry points for a "call Lelik" session.

Four routes, all Twilio-facing and all signature-verified via the injected
`signature_verifier` (`src/web/twilio_signature_verifier.py`): `POST
/voice/auth` (the inbound dial), `POST /voice/inbound-status` (the inbound
dial's lifecycle, configured on the number as "Call status changes"), `POST
/voice/answer` (the callback leg was picked up) and `POST /voice/status` (the
callback leg's lifecycle).

The callback is placed only once the inbound dial has ENDED. Originating it
from inside `/voice/auth` raced the caller's own line: the phone was still on
the inbound call, the carrier refused the second call at once (`no-answer` in
under a second) and the user got a missed-call SMS instead of Lelik.

`POST /voice/auth`:

1. Resolve the caller's number (`From`) to a user via `UserRepository`
   (RFC §4.6 identity resolution — phone is bound as platform "phone",
   `get_user_by_platform_id("phone", <E.164>)`). Unbound number -> reject
   the dial and post an operational alert. No ticket, no marker, no callback
   is ever produced for an unbound caller.
2. Enforce the RFC §3 one-call-per-user limit via the shared `EphemeralStore`
   marker at `voice_one_call:{user_id}`. A caller with a call already in
   flight or pending is rejected.
3. Mint a short-TTL ticket (`voice_ticket:{uuid}`) carrying the resolved
   identity, take the marker at the ticket's short TTL, and park the callback
   under the inbound `CallSid` (`voice_pending_callback:{CallSid}`). Answer
   with "Calling you back" + hangup.

`POST /voice/inbound-status`: on the inbound dial's `completed`, atomically
claim the parked callback (a duplicate status delivery finds nothing), extend
the marker to `one_call_ttl_s`, and have `LelikAgent` originate the callback;
then post the RFC §4.6 chat confirmation. Any other terminal status means the
dial never got as far as our answer — release the ticket and marker. If
origination raises, both are released and an alert posted. If no status ever
arrives, the short TTLs release everything within `_TICKET_TTL_S`.
"""
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from quart import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse

from src.domain.voice_auth_decision import AuthDecision
from src.utils.logger import logger

if TYPE_CHECKING:  # type-only: web/ must not import agents/ at runtime (REQ-ARCH-15)
    from src.agents.lelik_agent import LelikAgent

_TICKET_TTL_S = 300

# Twilio's `CallStatus` values that mean the call leg is over for good (as
# opposed to the non-terminal `initiated` / `ringing` / `in-progress` it also
# posts, because TwilioTelephonyAdapter subscribes to
# status_callback_event=["initiated", "ringing", "answered", "completed"]).
# Every one of these ends the one-call window, including the ones where
# /voice/answer is NEVER reached: a callback that rings out, hits a busy
# signal, or fails at the carrier produces no answer webhook at all, so this
# route is the only thing that can release the ticket and the marker before
# `one_call_ttl_s` (default 3600s) expires.
_TERMINAL_CALL_STATUSES = frozenset({"completed", "no-answer", "busy", "failed", "canceled"})

def create_voice_webhook_blueprint(
    user_repository,
    ephemeral_store,
    alert_sink,
    notification_service,
    lelik_agent_provider: Callable[[str], Awaitable[Optional["LelikAgent"]]],
    answer_url,
    signature_verifier,
    one_call_ttl_s: int = 3600,
    relay_stream_url: str = "",
) -> Blueprint:
    """Build the Twilio-facing voice webhook blueprint.

    `signature_verifier` is a REQUIRED async callable
    `(url, form_params, signature) -> bool`, injected the same way
    `voice_control_plane_app`'s `oidc_verifier` is: as a dependency, not an
    import, so the routes stay testable without a real Twilio auth token and
    the local-dev bypass policy lives in main.py's wiring. It is deliberately
    NOT optional-with-a-default — an authentication gate that silently
    disappears when a caller forgets to wire it is the exact failure class
    CLAUDE.md's `config.get()` note warns about.
    """
    bp = Blueprint("voice_webhook", __name__)

    async def _reject_if_unsigned(form) -> Optional[Response]:
        """403 unless the request carries a valid Twilio signature.

        Passes the EXTERNAL URL (including query string) — see `_external_url`
        — because that is what Twilio computed its HMAC over, and because the
        call ticket now rides in that query string.
        """
        signature = request.headers.get("X-Twilio-Signature", "")
        if await signature_verifier(_external_url(), dict(form), signature):
            return None
        logger.warning(f"voice webhook: rejected unsigned/invalid request to {request.path}")
        return Response("forbidden", status=403)

    @bp.route("/voice/auth", methods=["POST"])
    async def voice_auth():
        form = await request.form
        forbidden = await _reject_if_unsigned(form)
        if forbidden is not None:
            return forbidden
        caller = (form.get("From") or "").strip()

        profile = await user_repository.get_user_by_platform_id("phone", caller)
        if profile is None:
            logger.warning(f"voice auth: rejected dial from unbound number {caller}")
            await alert_sink.post(f"Voice: refused dial from unbound number {caller}")
            return _twiml_reject()

        marker_key = f"voice_one_call:{profile.user_id}"
        if await ephemeral_store.get(marker_key) is not None:
            logger.warning(f"voice auth: refused - {profile.user_id} already has a call in flight")
            return _twiml_reject()

        inbound_call_sid = (form.get("CallSid") or "").strip()
        if not inbound_call_sid:
            logger.warning(f"voice auth: dial from {profile.user_id} carried no CallSid, rejecting")
            return _twiml_reject()

        decision = AuthDecision(user_id=profile.user_id, account_id=profile.account_id)
        ticket = str(uuid.uuid4())
        await ephemeral_store.set(
            f"voice_ticket:{ticket}",
            {"user_id": decision.user_id, "account_id": decision.account_id},
            ttl_s=_TICKET_TTL_S,
        )
        # Short TTL while the callback is only parked: if Twilio never reports the
        # inbound dial as ended, the caller is locked out for minutes, not an hour.
        await ephemeral_store.set(marker_key, {"in_flight": True}, ttl_s=_TICKET_TTL_S)
        await ephemeral_store.set(
            _pending_key(inbound_call_sid),
            {
                "ticket": ticket,
                "user_id": decision.user_id,
                "account_id": decision.account_id,
                # The callback always returns to the exact number that dialed in (RFC §4.6).
                "to_number": caller,
            },
            ttl_s=_TICKET_TTL_S,
        )

        vr = VoiceResponse()
        vr.say("Calling you back.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    @bp.route("/voice/inbound-status", methods=["POST"])
    async def voice_inbound_status():
        """The inbound dial's lifecycle — the moment the caller's line is free."""
        form = await request.form
        forbidden = await _reject_if_unsigned(form)
        if forbidden is not None:
            return forbidden

        call_status = (form.get("CallStatus") or "").strip().lower()
        if call_status not in _TERMINAL_CALL_STATUSES:
            return Response("", status=200)

        # Atomic claim: Twilio retries status callbacks, and a second delivery must
        # not place a second call.
        pending = await ephemeral_store.get_and_delete(_pending_key((form.get("CallSid") or "").strip()))
        if pending is None:
            return Response("", status=200)

        ticket_key = f"voice_ticket:{pending['ticket']}"
        marker_key = f"voice_one_call:{pending['user_id']}"
        if call_status != "completed":
            logger.info(f"voice inbound-status: dial ended '{call_status}' before the answer, releasing")
            await ephemeral_store.delete(ticket_key)
            await ephemeral_store.delete(marker_key)
            return Response("", status=200)

        await ephemeral_store.set(marker_key, {"in_flight": True}, ttl_s=one_call_ttl_s)
        try:
            agent = await lelik_agent_provider(pending["user_id"])
            if agent is None:
                raise RuntimeError("voice companion is not configured on this deployment")
            await agent.execute(
                purpose="user asked to talk", ticket=pending["ticket"],
                answer_url=answer_url, to_number=pending["to_number"],
            )
        except Exception as exc:
            # A ticket/marker written for a callback that never went out must not
            # survive on its own until TTL expiry, or the user is locked out of any
            # retry for that whole window with no way to know why.
            logger.error(
                f"voice inbound-status: callback origination failed for {pending['user_id']}: {exc}",
                exc_info=True,
            )
            await ephemeral_store.delete(ticket_key)
            await ephemeral_store.delete(marker_key)
            await alert_sink.post(f"Voice: callback origination failed for user {pending['user_id']}: {exc}")
            return Response("", status=200)

        # RFC §4.6: every session opened posts to the user's chat channel.
        await notification_service.notify_raw(
            user_id=pending["user_id"],
            account_id=pending["account_id"],
            text="📞 Calling you back now",
        )
        return Response("", status=200)

    @bp.route("/voice/answer", methods=["POST"])
    async def voice_answer():
        """Twilio's webhook for the callback leg once it is answered.

        1. Resolve the ticket minted by `voice_auth` back to the caller's
           identity via `EphemeralStore`. A ticket that isn't there (already
           redeemed, expired, or forged) gets rejected outright - never open
           a session without a known user_id/account_id.

           **The ticket arrives in the QUERY STRING, not the form body.**
           `LelikAgent.execute` builds the callback URL as
           `f"{answer_url}?{urlencode({'ticket': ticket})}"`, and Twilio's
           callback POST carries only its OWN fields (`CallSid`, `AnsweredBy`,
           ...) in the body while leaving the configured URL's query string
           untouched. Reading it from `await request.form` therefore always
           produced an empty ticket and rejected EVERY real call - the seam
           neither `test_lelik_agent.py` (producer) nor
           `test_voice_webhook_app.py` (consumer, against a mocked form dict)
           crossed. `request.args` is a synchronous Quart property parsed from
           the URL; only `request.form` needs awaiting.
        2. AMD gate: if Twilio's `AnsweredBy` machine detection says a
           machine/voicemail picked up, hang up immediately without assembling
           a persona or opening a session - no point spending an LLM call on
           Lelik's prompt for an answering machine. The ticket and the one-call
           marker are RELEASED here (same two keys, same `ephemeral_store.
           delete` pattern as the persona-failure handler below): voicemail
           ends the call, so leaving them behind would lock the caller out of
           any retry for up to `one_call_ttl_s` (default 3600s). Resolving the
           ticket before this gate is what makes that possible and costs one
           ephemeral-store read, not an LLM call.
        3. Assemble Lelik's warm call-start session via
           `lelik_agent_provider(user_id)` -> `LelikAgent.session_config()` (RFC §4.8,
           decisions/lelik_warm_context.md), and stash instructions + tools back onto the
           ticket (relay's `/voice/session-config` reads it from there) before pointing
           the call at the relay's media-stream WebSocket.

        `build_for_agent` fails closed (repo convention: no fallback prompts) if
        Lelik's Firestore prompt content (token/blueprint/profile, Task 19) is
        missing or the assembly service raises for any other reason. Task 9 shipped
        this call with no error handling around it (bare 500 to Twilio on any
        raise); Task 19 closes that gap the same way `voice_auth`'s origination
        failure is handled: release the ticket and the one-call marker (so the
        caller is not locked out for `one_call_ttl_s`), post an operational alert,
        and answer with graceful TTS instead of a bare 500.
        """
        form = await request.form
        forbidden = await _reject_if_unsigned(form)
        if forbidden is not None:
            return forbidden
        answered_by = form.get("AnsweredBy", "")
        ticket = request.args.get("ticket", "")

        ticket_key = f"voice_ticket:{ticket}"
        identity = await ephemeral_store.get(ticket_key)
        if identity is None:
            logger.warning(f"voice answer: no identity found for ticket {ticket}, rejecting")
            return _twiml_reject()

        if answered_by.startswith("machine"):
            logger.info(
                f"voice answer: machine detected ({answered_by}) for ticket {ticket}, "
                "hanging up without opening a session"
            )
            await ephemeral_store.delete(ticket_key)
            await ephemeral_store.delete(f"voice_one_call:{identity['user_id']}")
            vr = VoiceResponse()
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        # A profile, fact-store or assembly failure is a persona failure, never a reason
        # to open a call on an empty context.
        try:
            agent = await lelik_agent_provider(identity["user_id"])
            if agent is None:
                raise RuntimeError("voice companion is not configured on this deployment")
            session = await agent.session_config(
                user_id=identity["user_id"], account_id=identity["account_id"],
            )
        except Exception as exc:
            # Same fail-open shape as voice_auth's origination-failure handling:
            # a ticket/marker that survives a failed persona assembly would lock
            # the caller out of any retry for up to one_call_ttl_s with no way to
            # know why.
            logger.error(
                f"voice answer: persona assembly failed for ticket {ticket}: {exc}",
                exc_info=True,
            )
            await ephemeral_store.delete(ticket_key)
            await ephemeral_store.delete(f"voice_one_call:{identity['user_id']}")
            await alert_sink.post(
                f"Voice: persona assembly failed for user {identity['user_id']}: {exc}"
            )
            return _twiml_persona_failed()

        # identity wins: /voice/delegate trusts user_id/account_id from this record.
        await ephemeral_store.set(ticket_key, {**session, **identity}, ttl_s=_TICKET_TTL_S)

        vr = VoiceResponse()
        connect = vr.connect()
        stream = connect.stream(url=relay_stream_url)
        stream.parameter(name="ticket", value=ticket)
        return Response(str(vr), mimetype="text/xml")

    @bp.route("/voice/status", methods=["POST"])
    async def voice_status():
        """Twilio's call-status callback for the outbound callback leg.

        `TwilioTelephonyAdapter.originate_call` has always subscribed to
        `status_callback_event=["initiated", "ringing", "answered",
        "completed"]` and `UserAgentFactory._build_lelik` has always pointed
        `status_callback_url` at `{service_url}/voice/status` - but the route
        itself did not exist, so every one of those callbacks 404'd. The
        consequence was not cosmetic: a callback that is dialed but never
        answered (rings out, busy, carrier-level failure) reaches neither
        `/voice/answer` nor `/voice/submit-transcript`, so NOTHING released the
        ticket or the one-call marker and the caller stayed locked out until
        `one_call_ttl_s` (default 3600s) expired.

        The ticket rides in this URL's query string, appended by
        `LelikAgent.execute` exactly like the answer callback's (and covered by
        the same Twilio signature, which is computed over the full URL). There
        is no other correlation path: Twilio's `CallSid` is only known AFTER
        `originate_call` returns, i.e. after the ticket was already minted, and
        mapping it back would need a second store write racing the `initiated`
        callback.

        Non-terminal statuses (`initiated`/`ringing`/`in-progress`) are
        acknowledged and ignored - releasing on those would free the marker
        while the call is still live. Twilio does not parse a TwiML body from a
        status callback, so this returns a bare 200.
        """
        form = await request.form
        forbidden = await _reject_if_unsigned(form)
        if forbidden is not None:
            return forbidden

        call_status = (form.get("CallStatus") or "").strip().lower()
        ticket = request.args.get("ticket", "")

        if call_status not in _TERMINAL_CALL_STATUSES:
            logger.info(f"voice status: non-terminal status '{call_status}' for ticket {ticket}, ignoring")
            return Response("", status=200)

        ticket_key = f"voice_ticket:{ticket}"
        identity = await ephemeral_store.get(ticket_key)
        if identity is None:
            # Already released by /voice/answer's AMD branch, the persona-failure
            # handler, /voice/session-config's single-use consumption, or a
            # duplicate delivery of this same callback. Nothing left to do.
            logger.info(
                f"voice status: terminal status '{call_status}' for ticket {ticket}, "
                "already released"
            )
            return Response("", status=200)

        logger.info(f"voice status: terminal status '{call_status}' for ticket {ticket}, releasing")
        await ephemeral_store.delete(ticket_key)
        await ephemeral_store.delete(f"voice_one_call:{identity['user_id']}")
        return Response("", status=200)

    return bp


def _pending_key(inbound_call_sid: str) -> str:
    return f"voice_pending_callback:{inbound_call_sid}"


def _external_url() -> str:
    """The absolute URL Twilio computed its signature over.

    Cloud Run terminates TLS at its front end and forwards plain HTTP to the
    container, and hypercorn here is started with no forwarded-header trust
    (`main.py`: bare `HypercornConfig()` with only `bind`/`use_reloader`/
    `accesslog`/`errorlog` set), so `request.url` reports `http://...` and would
    never match the `https://...` URL Twilio signed. Rebuild it from the
    `X-Forwarded-*` headers Cloud Run does set, falling back to the request's
    own scheme/host for local dev.

    The query string is included deliberately: it carries the call ticket, and
    Twilio's HMAC covers the full URL, so a swapped ticket invalidates the
    signature.
    """
    proto = (request.headers.get("X-Forwarded-Proto", "").split(",")[0].strip()
             or request.scheme)
    host = (request.headers.get("X-Forwarded-Host", "").split(",")[0].strip()
            or request.host)
    query = request.query_string.decode()
    return f"{proto}://{host}{request.path}" + (f"?{query}" if query else "")


def _twiml_reject() -> Response:
    vr = VoiceResponse()
    vr.reject()
    return Response(str(vr), mimetype="text/xml")


def _twiml_persona_failed() -> Response:
    """The callback was answered but Lelik's persona could not be assembled (e.g. the
    Firestore prompt content is missing, or the assembly service raised) - same
    plain-apology shape, its own function so the failure stays easy to find in logs/alerts."""
    vr = VoiceResponse()
    vr.say("Sorry, something went wrong setting up this call. Please try again shortly.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")
