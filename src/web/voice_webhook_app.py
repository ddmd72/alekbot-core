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
   marker at `voice_one_call:{user_id}`. A caller with a phone call already in
   flight or pending is rejected. A running WEB call of the same user is
   replaced instead (VOICE_CALL_CONTROL_CHANNEL_RFC §7.5.4; the phone is the
   urgent fallback, and a web page swiped away without "end call" must never
   block it): its media legs are closed as the web path closes them
   (`WebCallMedia`), the dial waits up to `_PHONE_REPLACE_WAIT_S` for the old
   call to release the marker, and goes on either way; step 3 then writes the
   phone call's marker over whatever is left. The marker names its call
   (`owner`), so the replaced call's late end-of-call report leaves the phone
   call's marker alone (`release_one_call_marker`).
3. Mint a short-TTL ticket (its keyed digest is the store key) carrying the resolved
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
import asyncio
import uuid
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from quart import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse

from src.domain.secret_digest import fingerprint
from src.domain.voice_auth_decision import AuthDecision
from src.domain.voice_ticket_keys import amd_key, ticket_key
from src.services.voice_call_setup_service import VoiceCallSetupError, VoiceCallSetupService
from src.utils.logger import logger
from src.web.voice_web_call_media import WebCallMedia

if TYPE_CHECKING:  # type-only: web/ must not import agents/ at runtime (REQ-ARCH-15)
    from src.agents.lelik_agent import LelikAgent

_TICKET_TTL_S = 300

# How long a phone dial waits for a replaced web call to release the one-call marker (VOICE_CALL_CONTROL_CHANNEL_RFC
# §7.5.4). Twilio gives `/voice/auth` about 15 s to answer before it fails the dial, and the user lookup, the store
# writes and a cold start come out of the same budget, so the web path's 30 s is impossible here. A web call already
# ending (the button pressed just before the dial, the relay finishing its report) releases within this bound; a live
# one does not (the relay's 5 s re-attach window alone is longer), and the dial then takes the marker over: the phone
# is the urgent fallback and is never refused because of a web call.
_PHONE_REPLACE_WAIT_S = 4.0
_PHONE_REPLACE_POLL_S = 0.25
# The share of that budget the media close may take. The SFU's own request timeout is 15 s (one for all legs, which
# close concurrently): a hung SFU must not spend the whole budget, and a close that has not finished by then is
# abandoned and the dial goes on. A web call left running then ends on its own (page, dead-peer watchdog); its release
# leaves the phone call's marker, and its ticket, if still unredeemed, is refused at session-config.
_PHONE_REPLACE_CLOSE_S = 1.5

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
    *,
    cipher,
    digest_key: bytes,
    media_room=None,
    replace_wait_s: float = _PHONE_REPLACE_WAIT_S,
    replace_poll_s: float = _PHONE_REPLACE_POLL_S,
    replace_close_s: float = _PHONE_REPLACE_CLOSE_S,
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

    `media_room` is the web call's SFU (the one the web-call blueprint gets): a phone dial closes a running web call's
    media legs through it. None where the web call is not deployed; a web call's marker is then only waited for and
    taken over.
    """
    bp = Blueprint("voice_webhook", __name__)
    call_setup = VoiceCallSetupService(ephemeral_store, alert_sink, lelik_agent_provider, digest_key=digest_key,
                                       ticket_ttl_s=_TICKET_TTL_S, one_call_ttl_s=one_call_ttl_s)
    web_calls = (WebCallMedia(media_room, ephemeral_store, call_setup, cipher, digest_key)
                 if media_room is not None else None)

    async def _replace_web_call(user_id: str, holder: dict) -> bool:
        """The dial met a one-call marker. True when it may go on: the holder is this user's web call, which is ended
        (RFC §7.5.4). A phone holder (its marker names no web call) is never replaced: False, the dial is refused."""
        web_call_id = holder.get("call_id")
        if not isinstance(web_call_id, str) or not web_call_id:
            return False
        logger.info(f"voice auth: phone dial of {user_id} replaces the running web call {web_call_id}")
        # One budget for the close and the wait together: neither may push the answer past Twilio's deadline.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + replace_wait_s
        if web_calls is not None:
            try:
                await asyncio.wait_for(_close_web_call(web_call_id, user_id),
                                       timeout=min(replace_close_s, replace_wait_s))
            except asyncio.TimeoutError:
                logger.warning(f"voice auth: closing the replaced web call {web_call_id} took over "
                               f"{min(replace_close_s, replace_wait_s):.1f}s, the dial goes on")
            except Exception:
                logger.error(f"voice auth: closing the replaced web call {web_call_id} failed, the dial goes on",
                             exc_info=True)
        try:
            released = await call_setup.wait_released(user_id, max(0.0, deadline - loop.time()), replace_poll_s)
        except Exception:
            logger.warning(f"voice auth: waiting for web call {web_call_id} to release failed, the phone call "
                           f"takes the marker over", exc_info=True)
            return True
        if not released:
            logger.info(f"voice auth: web call {web_call_id} still holds the marker after {replace_wait_s:.0f}s, "
                        f"the phone call takes it over")
        return True

    async def _close_web_call(web_call_id: str, user_id: str) -> None:
        # No record: the web call was hung up and is only finishing, or is still in setup; nothing to close.
        record = await web_calls.record(web_call_id, user_id)
        if record is not None:
            await web_calls.close(record, await web_calls.ticket(web_call_id, record), user_id)

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
            logger.info(f"voice auth: rejected dial from unbound number {caller}")
            await alert_sink.post(f"Voice: refused dial from unbound number {caller}")
            return _twiml_reject()

        # Checked before the marker: a dial that is refused anyway must not end a running web call.
        inbound_call_sid = (form.get("CallSid") or "").strip()
        if not inbound_call_sid:
            logger.warning(f"voice auth: dial from {profile.user_id} carried no CallSid, rejecting")
            return _twiml_reject()

        marker_key = f"voice_one_call:{profile.user_id}"
        holder = await ephemeral_store.get(marker_key)
        if holder is not None and not await _replace_web_call(profile.user_id, holder):
            logger.warning(f"voice auth: refused - {profile.user_id} already has a call in flight")
            return _twiml_reject()

        decision = AuthDecision(user_id=profile.user_id, account_id=profile.account_id)
        ticket = str(uuid.uuid4())
        await ephemeral_store.set(
            ticket_key(digest_key, ticket),
            {"user_id": decision.user_id, "account_id": decision.account_id},
            ttl_s=_TICKET_TTL_S,
        )
        # Short TTL while the callback is only parked: if Twilio never reports the
        # inbound dial as ended, the caller is locked out for minutes, not an hour.
        # Unconditional: after a replaced web call this is the takeover. The marker names
        # this call, so that web call's late release leaves it alone.
        await ephemeral_store.set(marker_key, {"in_flight": True, **call_setup.owner_of(ticket)}, ttl_s=_TICKET_TTL_S)
        pending_key = _pending_key(inbound_call_sid)
        await ephemeral_store.set(
            pending_key,
            {
                # The callback needs the ticket back to build its URL: ciphertext bound to this record.
                "ticket_ct": await cipher.encrypt(ticket, aad=f"voice/{pending_key}"),
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
        pending_key = _pending_key((form.get("CallSid") or "").strip())
        pending = await ephemeral_store.get_and_delete(pending_key)
        if pending is None:
            return Response("", status=200)

        pending_ticket = await cipher.decrypt(pending["ticket_ct"], aad=f"voice/{pending_key}")
        redeem_key = ticket_key(digest_key, pending_ticket)
        marker_key = f"voice_one_call:{pending['user_id']}"
        if call_status != "completed":
            logger.info(f"voice inbound-status: dial ended '{call_status}' before the answer, releasing")
            await ephemeral_store.delete(redeem_key)
            await ephemeral_store.delete(marker_key)
            return Response("", status=200)

        # Rewritten whole, owner included: this also restores the marker if a replaced web call's release deleted it
        # in the instant between its ownership read and its delete (`release_one_call_marker`).
        await ephemeral_store.set(marker_key, {"in_flight": True, **call_setup.owner_of(pending_ticket)},
                                  ttl_s=one_call_ttl_s)
        try:
            agent = await lelik_agent_provider(pending["user_id"])
            if agent is None:
                raise RuntimeError("voice companion is not configured on this deployment")
            await agent.execute(
                purpose="user asked to talk", ticket=pending_ticket,
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
            await ephemeral_store.delete(redeem_key)
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

        redeem_key = ticket_key(digest_key, ticket)
        identity = await ephemeral_store.get(redeem_key)
        if identity is None:
            logger.warning(f"voice answer: no identity found for ticket {fingerprint(ticket)}, rejecting")
            return _twiml_reject()

        # Sync-AMD path only: with async AMD (the adapter's mode) Twilio sends no AnsweredBy
        # here, and the verdict lands on /voice/status instead.
        if answered_by.startswith("machine"):
            logger.info(
                f"voice answer: machine detected ({answered_by}) for ticket {fingerprint(ticket)}, "
                "hanging up without opening a session"
            )
            await ephemeral_store.delete(redeem_key)
            await ephemeral_store.delete(f"voice_one_call:{identity['user_id']}")
            vr = VoiceResponse()
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        # A profile, fact-store or assembly failure is a persona failure, never a reason
        # to open a call on an empty context.
        try:
            await call_setup.prepare(ticket, identity["user_id"], identity["account_id"], "phone")
        except VoiceCallSetupError:
            return _twiml_persona_failed()

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

        answered_by = form.get("AnsweredBy")
        if answered_by and not call_status:
            # Async AMD verdict (TwilioTelephonyAdapter points it here). The call is already
            # live, so it is only recorded; /voice/submit-transcript weighs it at call end.
            logger.info(f"voice status: AMD verdict '{answered_by}' for ticket {fingerprint(ticket)}")
            await ephemeral_store.set(amd_key(digest_key, ticket), {"answered_by": answered_by}, ttl_s=one_call_ttl_s)
            return Response("", status=200)

        if call_status not in _TERMINAL_CALL_STATUSES:
            logger.info(f"voice status: non-terminal status '{call_status}' for ticket {fingerprint(ticket)}, ignoring")
            return Response("", status=200)

        redeem_key = ticket_key(digest_key, ticket)
        identity = await ephemeral_store.get(redeem_key)
        if identity is None:
            # Already released by /voice/answer's AMD branch, the persona-failure
            # handler, /voice/session-config's single-use consumption, or a
            # duplicate delivery of this same callback. Nothing left to do.
            logger.info(
                f"voice status: terminal status '{call_status}' for ticket {fingerprint(ticket)}, "
                "already released"
            )
            return Response("", status=200)

        logger.info(f"voice status: terminal status '{call_status}' for ticket {fingerprint(ticket)}, releasing")
        await ephemeral_store.delete(redeem_key)
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
