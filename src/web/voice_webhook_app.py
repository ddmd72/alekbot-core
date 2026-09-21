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
   originate the actual callback.
4. Per RFC §4.6 ("every session opened posts to the user's chat channel"),
   fire a short `notify_raw` confirmation once the callback has been
   originated, so the user sees this on their bound Slack/Telegram channel
   even though the interaction itself is happening over voice.
"""
import uuid

from quart import Blueprint, Response, request
from twilio.twiml.voice_response import VoiceResponse

from src.domain.voice_auth_decision import AuthDecision
from src.utils.logger import logger

_TICKET_TTL_S = 300


def create_voice_webhook_blueprint(
    user_repository,
    ephemeral_store,
    alert_sink,
    notification_service,
    lelik_agent_factory,
    answer_url,
    one_call_ttl_s: int = 3600,
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
        await ephemeral_store.set(
            f"voice_ticket:{ticket}",
            {"user_id": decision.user_id, "account_id": decision.account_id},
            ttl_s=_TICKET_TTL_S,
        )
        await ephemeral_store.set(marker_key, {"in_flight": True}, ttl_s=one_call_ttl_s)

        agent = lelik_agent_factory(user_id=decision.user_id, account_id=decision.account_id)
        await agent.execute(purpose="user asked to talk", ticket=ticket, answer_url=answer_url)

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

    return bp


def _twiml_reject() -> Response:
    vr = VoiceResponse()
    vr.reject()
    return Response(str(vr), mimetype="text/xml")
