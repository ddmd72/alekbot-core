"""
Cabinet web call — the browser side of VOICE_WEB_TRANSPORT_RFC §5.2.

The page never sees the call ticket: it holds a separate `call_id`, and every route checks the
call record's owner against the Cabinet JWT. The relay is reached only by the SFU, which dials the
ticket-carrying `/sfu/*` URLs this blueprint hands it.
"""
import os
import uuid

from quart import Blueprint, g, jsonify, request, send_file

from src.ports.media_room_port import MediaRoomError
from src.services.voice_call_setup_service import VoiceCallSetupError
from src.utils.logger import logger
from src.web.cabinet_auth import make_auth_required

_TICKET_TTL_S = 300


def create_voice_web_call_blueprint(session_service, call_setup, media_room, ephemeral_store,
                                    relay_base_url: str, call_ttl_s: int = 3600) -> Blueprint:
    bp = Blueprint("voice_web_call", __name__)
    auth_required = make_auth_required(session_service)

    async def _owned(call_id: str):
        record = await ephemeral_store.get(f"voice_web_call:{call_id}")
        if record is None or record.get("user_id") != g.user_id:
            return None
        return record

    @bp.route("/cabinet/call")
    async def call_page():
        response = await send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "call.html"))
        # Revalidate on every load: send_file's 12 h max-age served a stale page after deploys.
        response.headers["Cache-Control"] = "no-cache"
        return response

    @bp.route("/api/voice/web-call", methods=["POST"])
    @auth_required
    async def start():
        body = await request.get_json()
        # Validated BEFORE claim: a malformed body must never hold the one-call marker (or a
        # ticket) for its TTL just to be rejected. "mid present" mirrors what str(body["mid"])
        # below needs — any non-None value, not necessarily non-empty (mid "0" is common).
        if (not isinstance(body, dict) or not isinstance(body.get("sdp"), str) or not body.get("sdp")
                or body.get("mid") is None):
            return jsonify({"error": "invalid request"}), 400
        call_id, ticket = uuid.uuid4().hex, str(uuid.uuid4())
        if not await call_setup.claim(g.user_id, {"call_id": call_id}, ttl_s=_TICKET_TTL_S):
            return jsonify({"error": "a call is already in progress"}), 409
        try:
            await call_setup.prepare(ticket, g.user_id, g.account_id, "web")
        except VoiceCallSetupError:
            return jsonify({"error": "could not prepare the call"}), 503
        # One try around both the SFU call and the record write: an SFU timeout/network error
        # (not just a MediaRoomError) or a store failure on the write must both release the
        # marker and ticket rather than escape as a bare 500 and hold them for the ticket's
        # full 300s TTL.
        try:
            leg = await media_room.open_caller(body["sdp"], str(body["mid"]))
            await ephemeral_store.set(f"voice_web_call:{call_id}", {
                "user_id": g.user_id, "account_id": g.account_id, "ticket": ticket,
                "session_id": leg.session_id, "adapter_ids": [],
            }, ttl_s=call_ttl_s)
        except Exception:
            logger.error(f"web call: could not start the call for {g.user_id}", exc_info=True)
            await call_setup.release(ticket, g.user_id)
            return jsonify({"error": "media service unavailable"}), 502
        # The marker stays at the short setup-window TTL from `claim` above — it is extended to
        # the full call TTL only once the relay actually redeems the ticket
        # (`voice_control_plane_app.session_config`), not just because the offer/answer
        # succeeded here. Extending it this early would hold the marker for up to an hour even
        # if the relay never shows up.
        return jsonify({"call_id": call_id, "sdp": leg.answer_sdp}), 201

    @bp.route("/api/voice/web-call/<call_id>/connect", methods=["POST"])
    @auth_required
    async def connect(call_id):
        record = await _owned(call_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        ticket = record["ticket"]
        try:
            leg = await media_room.attach_agent(
                record["session_id"],
                f"{relay_base_url}/sfu/ingest?ticket={ticket}",
                f"{relay_base_url}/sfu/egress?ticket={ticket}",
            )
        except MediaRoomError:
            logger.error(f"web call {call_id}: attaching the relay failed", exc_info=True)
            return jsonify({"error": "media service unavailable"}), 502
        # attach_agent is several SFU round-trips; a hangup in that window deletes the record
        # and closes the adapters it listed (none yet). Writing the record back here would
        # resurrect a hung-up call and leave these adapters dialling the relay.
        if await _owned(call_id) is None:
            logger.info(f"web call {call_id}: hung up while the relay was being attached")
            await media_room.close(leg.adapter_ids)
            return jsonify({"error": "not found"}), 404
        await ephemeral_store.set(f"voice_web_call:{call_id}", {**record, "adapter_ids": leg.adapter_ids},
                                  ttl_s=call_ttl_s)
        return jsonify({"sdp": leg.offer_sdp})

    @bp.route("/api/voice/web-call/<call_id>/renegotiate", methods=["POST"])
    @auth_required
    async def renegotiate(call_id):
        record = await _owned(call_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        body = await request.get_json()
        try:
            await media_room.complete_negotiation(record["session_id"], body["sdp"])
        except MediaRoomError:
            return jsonify({"error": "media service unavailable"}), 502
        return jsonify({"ok": True})

    @bp.route("/api/voice/web-call/<call_id>/hangup", methods=["POST"])
    @auth_required
    async def hangup(call_id):
        record = await _owned(call_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        await media_room.close(record.get("adapter_ids", []))
        # Ticket still unredeemed = the relay never started: nothing will release the marker.
        if await ephemeral_store.get(f"voice_ticket:{record['ticket']}") is not None:
            await call_setup.release(record["ticket"], g.user_id)
        # The record goes last: a connect still attaching re-reads it and cleans up after itself.
        await ephemeral_store.delete(f"voice_web_call:{call_id}")
        return jsonify({"ok": True})

    @bp.route("/api/voice/web-call/<call_id>/status", methods=["GET"])
    @auth_required
    async def status(call_id):
        record = await _owned(call_id)
        if record is None:
            return jsonify({"error": "not found"}), 404
        holder = await call_setup.holder(g.user_id)
        live = holder is not None and holder.get("call_id") == call_id
        return jsonify({"state": "live" if live else "ended"})

    return bp
