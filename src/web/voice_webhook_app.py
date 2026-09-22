"""
Voice auth webhook — Twilio's entry point for an inbound "call Lelik" dial.

`POST /voice/auth` (Twilio's default webhook method — Quart has no GET-only
restriction, unlike the raw HTTP parser spike 0.2 hit on the `websockets`
library, so this stays on POST and needs no Twilio number-config change; see
`docs/07_deployment/SCHEDULERS.md`-style deployment notes in Task 14):

1. Resolve the caller's number (`From`) to a user via `UserRepository`
   (RFC §4.6 identity resolution — phone is bound as platform "phone",
   `get_user_by_platform_id("phone", <E.164>)`). Unbound number -> reject
   the dial and post an operational alert. No ticket, no marker, no callback
   is ever produced for an unbound caller.
2. Enforce the RFC §3 one-call-per-user limit via the shared `EphemeralStore`
   marker at `voice_one_call:{user_id}` (released by
   `voice_control_plane_app.submit_transcript` at end-of-call). A caller with
   a call already in flight is rejected, no callback originated.
3. Mint a short-TTL ticket (`voice_ticket:{uuid}`) carrying the resolved
   identity — the relay later redeems it via `POST /voice/session-config` —
   and the one-call marker, then hand off to the `LelikAgent` (Task 10) to
   originate the actual callback. If origination raises, both the ticket
   and the marker are deleted immediately (mint-side counterpart to
   `voice_control_plane_app.submit_transcript`'s release-on-failure
   guarantee, commit d1dd648) — otherwise the marker would strand the user
   locked out of any retry for up to `one_call_ttl_s` (default 3600s) with
   no alert. An operational alert is posted and a graceful TwiML response
   returned instead of letting the exception surface as a bare 500 to
   Twilio.
4. Per RFC §4.6 ("every session opened posts to the user's chat channel"),
   fire a short `notify_raw` confirmation once the callback has been
   originated, so the user sees this on their bound Slack/Telegram channel
   even though the interaction itself is happening over voice.
"""
import uuid

from quart import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse

from src.domain.entities import FactDomain
from src.domain.voice_auth_decision import AuthDecision
from src.utils.logger import logger

_TICKET_TTL_S = 300

# RFC §4.8: Lelik reads "a small explicit list of fact domains ... enough for light
# continuity and small talk, not a substitute for forwarding" — NOT Alek's whole
# biographical cache. A fat context is what nudges a companion into answering from
# its own mouth, which is precisely what §4.2's forwarding rule exists to prevent.
#
# AGENT_DIRECTIVE is deliberately in this list and MUST NOT be "cleaned up" out of
# it. It is not small-talk content: `PromptBuilder.build_for_agent` extracts the
# standing-directive block by filtering the *same* list that is passed in as
# `biographical_facts` (`src/services/prompt_builder.py` — `directive_facts = [f for
# f in biographical_facts if _domain(f) == FactDomain.AGENT_DIRECTIVE.value]`).
# Dropping AGENT_DIRECTIVE here silently kills RFC §4.2's standing-directive
# backstop while `include_directives=True` below still reads as if it were on.
_LELIK_FACT_DOMAINS = frozenset({
    FactDomain.BIOGRAPHICAL.value,
    FactDomain.PREFERENCE.value,
    FactDomain.LOCATION.value,
    FactDomain.AGENT_DIRECTIVE.value,
})


def create_voice_webhook_blueprint(
    user_repository,
    ephemeral_store,
    alert_sink,
    notification_service,
    lelik_agent_factory,
    answer_url,
    one_call_ttl_s: int = 3600,
    prompt_builder=None,
    fact_repository=None,
    relay_stream_url: str = "",
) -> Blueprint:
    bp = Blueprint("voice_webhook", __name__)

    @bp.route("/voice/auth", methods=["POST"])
    async def voice_auth():
        form = await request.form
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

        decision = AuthDecision(user_id=profile.user_id, account_id=profile.account_id)
        ticket = str(uuid.uuid4())
        ticket_key = f"voice_ticket:{ticket}"
        await ephemeral_store.set(
            ticket_key,
            {"user_id": decision.user_id, "account_id": decision.account_id},
            ttl_s=_TICKET_TTL_S,
        )
        await ephemeral_store.set(marker_key, {"in_flight": True}, ttl_s=one_call_ttl_s)

        try:
            # to_number is the caller's own E.164 number (RFC §4.6: the callback
            # always returns to the exact number that dialed in) — the same value
            # already used for the platform-identity lookup above. lelik_agent_factory
            # is expected to be `UserAgentFactory._build_lelik(user_id, account_id,
            # to_number)` (or a callable matching that 3-arg shape), not the 2-arg
            # `(user_id, account_id)` this call site used before to_number existed.
            agent = lelik_agent_factory(
                user_id=decision.user_id, account_id=decision.account_id, to_number=caller,
            )
            await agent.execute(purpose="user asked to talk", ticket=ticket, answer_url=answer_url)
        except Exception as exc:
            # Mint-side counterpart to submit_transcript's release-on-failure guarantee
            # (voice_control_plane_app.py) - a ticket/marker written here but never
            # redeemed by a live call must not survive on its own until TTL expiry
            # (up to one_call_ttl_s, default 3600s), or the user is locked out of any
            # retry for that whole window with no way to know why.
            logger.error(
                f"voice auth: callback origination failed for {decision.user_id}: {exc}",
                exc_info=True,
            )
            await ephemeral_store.delete(ticket_key)
            await ephemeral_store.delete(marker_key)
            await alert_sink.post(f"Voice: callback origination failed for user {decision.user_id}: {exc}")
            return _twiml_origination_failed()

        # RFC §4.6: every session opened posts to the user's chat channel.
        await notification_service.notify_raw(
            user_id=decision.user_id,
            account_id=decision.account_id,
            text="📞 Calling you back now",
        )

        vr = VoiceResponse()
        vr.say("Calling you back.")
        vr.hangup()
        return Response(str(vr), mimetype="text/xml")

    @bp.route("/voice/answer", methods=["POST"])
    async def voice_answer():
        """Twilio's webhook for the callback leg once it is answered.

        1. AMD gate: if Twilio's `AnsweredBy` machine detection says a
           machine/voicemail picked up, hang up immediately without ever
           resolving the ticket or opening a persona/session - no point
           spending an LLM call assembling Lelik's prompt for an answering
           machine.
        2. Resolve the ticket minted by `voice_auth` back to the caller's
           identity via `EphemeralStore`. A ticket that isn't there (already
           redeemed, expired, or forged) gets rejected outright - never open
           a session without a known user_id/account_id.
        3. Assemble Lelik's persona via `PromptBuilderPort.build_for_agent`,
           over a biographical read scoped to `_LELIK_FACT_DOMAINS` (RFC §4.8),
           and stash it back onto the ticket (relay's `/voice/session-config`
           reads it from there, Task 5/Task 4 territory) before pointing the
           call at the relay's media-stream WebSocket.

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
        answered_by = form.get("AnsweredBy", "")
        ticket = form.get("ticket", "")

        if answered_by.startswith("machine"):
            logger.info(
                f"voice answer: machine detected ({answered_by}) for ticket {ticket}, "
                "hanging up without opening a session"
            )
            vr = VoiceResponse()
            vr.hangup()
            return Response(str(vr), mimetype="text/xml")

        ticket_key = f"voice_ticket:{ticket}"
        identity = await ephemeral_store.get(ticket_key)
        if identity is None:
            logger.warning(f"voice answer: no identity found for ticket {ticket}, rejecting")
            return _twiml_reject()

        # RFC §4.8's domain scoping, implemented through the kwarg that actually
        # exists. `build_for_agent` has no `session_domains` parameter (that belongs
        # to CompanionContextAssemblerService.assemble_context, which is not wired
        # into this webhook) — the real override point is `biographical_facts`:
        # when it is non-None, PromptBuilder uses the list verbatim instead of
        # fetching the full cache itself. So `include_biographical=True` here means
        # "use exactly this list", not "go fetch everything".
        # include_directives=True is §4.2's standing-directive backstop: the rulebook
        # is the lever to reach for if the persona's forwarding discipline slips —
        # and it is fed from the same scoped list, hence AGENT_DIRECTIVE's presence
        # in _LELIK_FACT_DOMAINS (see the constant's comment).
        #
        # The fact fetch sits inside the persona-assembly try/except on purpose: a
        # repository failure is a persona-assembly failure, not a reason to silently
        # open a call on an empty context.
        try:
            all_facts = await fact_repository.get_biographical_context_cached(
                identity["account_id"]
            )
            scoped_facts = [
                f for f in (all_facts or [])
                if isinstance(f, dict) and f.get("domain") in _LELIK_FACT_DOMAINS
            ]
            instructions = await prompt_builder.build_for_agent(
                agent_type="lelik",
                user_id=identity["user_id"],
                account_id=identity["account_id"],
                biographical_facts=scoped_facts,
                include_biographical=True,
                include_directives=True,
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

        await ephemeral_store.set(
            ticket_key,
            {**identity, "instructions": instructions},
            ttl_s=_TICKET_TTL_S,
        )

        vr = VoiceResponse()
        connect = vr.connect()
        stream = connect.stream(url=relay_stream_url)
        stream.parameter(name="ticket", value=ticket)
        return Response(str(vr), mimetype="text/xml")

    return bp


def _twiml_reject() -> Response:
    vr = VoiceResponse()
    vr.reject()
    return Response(str(vr), mimetype="text/xml")


def _twiml_origination_failed() -> Response:
    """The dial was authenticated (unlike _twiml_reject's unbound/in-flight cases) but the
    callback itself failed to originate - tell the caller plainly rather than silently
    hanging up on them, then end the call cleanly (no dangling <Reject> since this wasn't
    a rejection of the caller)."""
    vr = VoiceResponse()
    vr.say("Sorry, something went wrong placing your callback. Please try again shortly.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")


def _twiml_persona_failed() -> Response:
    """The callback was answered but Lelik's persona could not be assembled (e.g. the
    Firestore prompt content is missing, or the assembly service raised) - same
    plain-apology shape as _twiml_origination_failed, distinct call site so the two
    failure classes stay easy to tell apart in logs/alerts."""
    vr = VoiceResponse()
    vr.say("Sorry, something went wrong setting up this call. Please try again shortly.")
    vr.hangup()
    return Response(str(vr), mimetype="text/xml")
