"""
Cabinet web call — the browser side of VOICE_WEB_TRANSPORT_RFC §5.2.

The page never sees the call ticket: it holds a separate `call_id`, and every route checks the
call record's owner against the Cabinet JWT. The relay is reached only by the SFU, which dials the
ticket-carrying `/sfu/*` URLs this blueprint hands it.

The page reaches the relay directly only on the control channel (VOICE_CALL_CONTROL_CHANNEL_RFC §4.1): a per-call
token minted at start, its SHA-256 on the ticket record, the token itself encrypted on the call record and handed out
by `/connect` as `control: {url, token}` while the `VOICE_CONTROL_CHANNEL` knob is on. The token is never logged.

A start that meets the user's own running web call replaces it (RFC §7.5): that call's media legs are closed as
`/hangup` closes them, but without the button marker, so the relay's end-of-call report keeps its resume handle; the
start waits for the one-call marker to be released and then claims it, so the new call's session config resumes.
"""
import asyncio
import json
import os
import secrets
import uuid

from quart import Blueprint, Response, g, jsonify, request, send_file

from src.domain.voice_control_protocol import control_token_digest
from src.domain.voice_mode import UnsupportedVoiceMode, VoiceMode
from src.domain.voice_resume import BUTTON_END_TTL_S
from src.domain.voice_ticket_keys import button_end_key, ticket_key
from src.ports.media_room_port import MediaRoomError
from src.services.voice_call_setup_service import VoiceCallSetupError
from src.utils.logger import logger
from src.web.cabinet_auth import make_auth_required

_TICKET_TTL_S = 300
_ICON_SIZES = (180, 192, 512)
# Modes with their own page, mapped to their Home Screen name (manifest name and short_name, title meta).
_MODE_PAGES = {VoiceMode.INTERPRETER: "Lelik Translator", VoiceMode.TUTOR: "Lelik Tutor"}


# Closed set of /hangup triggers the page may report (VOICE_CALL_CONTROL_CHANNEL_RFC §7.1).
_HANGUP_TRIGGERS = frozenset({"button", "status_ended", "start_failed", "superseded"})

# How long a replacing start waits for the running call's one-call marker to be released (RFC §7.5.2). The release is
# the last step of the relay's end-of-call report: the SFU re-attach window (5 s) passes first, then the relay's
# teardown and the report itself, and on a companion (lelik-mode) call the report runs the end-of-call summary, an LLM
# call, before it releases. Thirty seconds covers that with room and stays well under Quart's 60 s response timeout;
# the page's start request has no timeout of its own. Past the bound the start answers 409 as before.
_REPLACE_WAIT_S = 30.0
_REPLACE_POLL_S = 0.25


def _control_channel_on() -> bool:
    """Kill switch of the control channel (RFC §9, §12): an optional knob, read per request. Off (the default) means
    `/connect` hands out no `control`, so the page never opens the socket."""
    return os.getenv("VOICE_CONTROL_CHANNEL", "off").strip().lower() == "on"


def create_voice_web_call_blueprint(session_service, call_setup, media_room, ephemeral_store,
                                    relay_base_url: str, call_ttl_s: int = 3600, *, cipher,
                                    digest_key: bytes, replace_wait_s: float = _REPLACE_WAIT_S,
                                    replace_poll_s: float = _REPLACE_POLL_S) -> Blueprint:
    bp = Blueprint("voice_web_call", __name__)
    auth_required = make_auth_required(session_service)

    async def _owned(call_id: str):
        record = await ephemeral_store.get(f"voice_web_call:{call_id}")
        if record is None or record.get("user_id") != g.user_id:
            return None
        return record

    async def _ticket_of(call_id: str, record: dict) -> str:
        return await cipher.decrypt(record["ticket_ct"], aad=f"voice/voice_web_call:{call_id}")

    def _control_aad(call_id: str) -> str:
        # Its own AAD: the two ciphertexts of one record cannot stand in for each other.
        return f"voice/voice_web_call:{call_id}/control"

    async def _close_media(record: dict, ticket: str) -> None:
        """End a call's media: the relay sees its egress gone and runs the normal end path. Shared by `/hangup` and
        the replacing start; neither the button marker nor the record is touched here."""
        await media_room.close(record.get("adapter_ids", []))
        # Ticket still unredeemed = the relay never started: nothing will release the marker.
        if await ephemeral_store.get(ticket_key(digest_key, ticket)) is not None:
            await call_setup.release(ticket, g.user_id)

    async def _replace_running_call(call_id: str) -> bool:
        """The claim for `call_id` was refused. When the holder is this user's own web call, end it and claim once its
        marker is released (RFC §7.5); True when claimed. A phone call (its marker names no web call) is never
        replaced. The marker is per user, so the holder is always this user's call.

        The replaced call's record stays: a page still showing it sees `ended` on its status poll and hangs up with
        `status_ended`, which writes no button marker either."""
        holder = await call_setup.holder(g.user_id)
        if holder is None:  # ended between the refused claim and this read
            return await call_setup.claim(g.user_id, {"call_id": call_id}, ttl_s=_TICKET_TTL_S)
        old_call_id = holder.get("call_id")
        record = await _owned(old_call_id) if isinstance(old_call_id, str) and old_call_id else None
        if record is None:
            return False
        logger.info(f"web call: replacing the running call {old_call_id} of {g.user_id} with {call_id}")
        try:
            await _close_media(record, await _ticket_of(old_call_id, record))
        except Exception:
            logger.error(f"web call {old_call_id}: closing the replaced call failed", exc_info=True)
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + replace_wait_s
        while await call_setup.holder(g.user_id) is not None:
            if loop.time() >= deadline:
                logger.warning(f"web call {old_call_id}: still running {replace_wait_s:.0f}s after it was replaced, "
                               f"the new call is refused")
                return False
            await asyncio.sleep(replace_poll_s)
        return await call_setup.claim(g.user_id, {"call_id": call_id}, ttl_s=_TICKET_TTL_S)

    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")

    @bp.route("/cabinet/call")
    async def call_page():
        response = await send_file(os.path.join(static_dir, "call.html"))
        # Revalidate on every load: send_file's 12 h max-age served a stale page after deploys.
        response.headers["Cache-Control"] = "no-cache"
        return response

    # Mode pages: call.html with the mode injected (its manifest link swapped, a one-line script
    # setting the mode). Explicit routes per mode: a catch-all would shadow the icon/manifest routes.
    def _mode_page(mode: VoiceMode, title: str):
        async def view():
            with open(os.path.join(static_dir, "call.html"), encoding="utf-8") as f:
                html = f.read()
            html = html.replace("/cabinet/call/manifest.webmanifest", f"/cabinet/call/{mode.value}/manifest.webmanifest", 1)
            html = html.replace("/cabinet/call/icon-180.png", f"/cabinet/call/{mode.value}/icon-180.png", 1)
            html = html.replace('<meta name="apple-mobile-web-app-title" content="Lelik" />',
                                f'<meta name="apple-mobile-web-app-title" content="{title}" />', 1)
            html = html.replace("</head>", f'<script>window.VOICE_MODE = "{mode.value}";</script>\n  </head>', 1)
            return Response(html, mimetype="text/html", headers={"Cache-Control": "no-cache"})
        return view

    def _manifest(start_url: str, name: str, short_name: str, icon_prefix: str = "/cabinet/call"):
        async def view():
            with open(os.path.join(static_dir, "call.webmanifest"), encoding="utf-8") as f:
                manifest = json.load(f)
            manifest.update(name=name, short_name=short_name, start_url=start_url)
            for icon in manifest["icons"]:
                icon["src"] = icon["src"].replace("/cabinet/call", icon_prefix, 1)
            return Response(json.dumps(manifest), mimetype="application/manifest+json")
        return view

    def _mode_icon(mode: VoiceMode):
        # Public like call_icon: iOS fetches icons without the session cookie.
        async def view(size: int):
            if size not in _ICON_SIZES:
                return jsonify({"error": "not found"}), 404
            return await send_file(os.path.join(static_dir, f"{mode.value}-icon-{size}.png"), mimetype="image/png")
        return view

    for _mode, _title in _MODE_PAGES.items():
        bp.add_url_rule(f"/cabinet/call/{_mode.value}", f"call_page_{_mode.value}", _mode_page(_mode, _title))
        bp.add_url_rule(f"/cabinet/call/{_mode.value}/manifest.webmanifest", f"call_manifest_{_mode.value}",
                        _manifest(f"/cabinet/call/{_mode.value}", _title, _title,
                                  icon_prefix=f"/cabinet/call/{_mode.value}"))
        bp.add_url_rule(f"/cabinet/call/{_mode.value}/icon-<int:size>.png", f"call_icon_{_mode.value}",
                        _mode_icon(_mode))

    @bp.route("/cabinet/call/modes")
    async def call_modes_page():
        response = await send_file(os.path.join(static_dir, "call_modes.html"))
        response.headers["Cache-Control"] = "no-cache"
        return response

    bp.add_url_rule("/cabinet/call/modes/manifest.webmanifest", "call_modes_manifest",
                    _manifest("/cabinet/call/modes", "Lelik · Modes", "Modes"))

    # Home Screen install (the app has no static folder, so these are explicit routes). Public
    # on purpose: iOS fetches the manifest and icons without the session cookie.
    @bp.route("/cabinet/call/manifest.webmanifest")
    async def call_manifest():
        return await send_file(os.path.join(static_dir, "call.webmanifest"),
                               mimetype="application/manifest+json")

    @bp.route("/cabinet/call/icon-<int:size>.png")
    async def call_icon(size: int):
        if size not in _ICON_SIZES:
            return jsonify({"error": "not found"}), 404
        return await send_file(os.path.join(static_dir, f"call-icon-{size}.png"), mimetype="image/png")

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
        try:
            mode = VoiceMode(body.get("mode", VoiceMode.LELIK.value))
        except ValueError:
            return jsonify({"error": "unknown_mode"}), 400
        call_id, ticket = uuid.uuid4().hex, str(uuid.uuid4())
        # Minted at start, not at connect: the SFU dials the relay during attach_agent and the relay redeems the
        # ticket at once, so the digest must already be on the ticket record (RFC §4.1). Minted whatever the knob
        # says, so turning the knob on or off acts at /connect alone.
        control_token = secrets.token_urlsafe(32)
        if (not await call_setup.claim(g.user_id, {"call_id": call_id}, ttl_s=_TICKET_TTL_S)
                and not await _replace_running_call(call_id)):
            return jsonify({"error": "a call is already in progress"}), 409
        try:
            await call_setup.prepare(ticket, g.user_id, g.account_id, "web", mode,
                                     control_token_sha256=control_token_digest(control_token))
        except UnsupportedVoiceMode:
            # prepare has already released the marker and ticket, exactly as for its other failures.
            return jsonify({"error": "mode_unavailable"}), 409
        except VoiceCallSetupError:
            return jsonify({"error": "could not prepare the call"}), 503
        # One try around both the SFU call and the record write: an SFU timeout/network error
        # (not just a MediaRoomError) or a store failure on the write must both release the
        # marker and ticket rather than escape as a bare 500 and hold them for the ticket's
        # full 300s TTL.
        try:
            leg = await media_room.open_caller(body["sdp"], str(body["mid"]))
            await ephemeral_store.set(f"voice_web_call:{call_id}", {
                "user_id": g.user_id, "account_id": g.account_id, "ticket_ct": await cipher.encrypt(ticket, aad=f"voice/voice_web_call:{call_id}"),
                "session_id": leg.session_id, "adapter_ids": [],
                "control_ct": await cipher.encrypt(control_token, aad=_control_aad(call_id)),
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
        ticket = await _ticket_of(call_id, record)
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
        body = {"sdp": leg.offer_sdp}
        # Same relay base as the SFU URLs above: the page's socket reaches the process that runs this call.
        # A record from before the token existed has no `control_ct`, and gets no channel.
        if _control_channel_on() and record.get("control_ct"):
            body["control"] = {"url": f"{relay_base_url}/ctl",
                               "token": await cipher.decrypt(record["control_ct"], aad=_control_aad(call_id))}
        return jsonify(body)

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
        # The body is optional: pages from before the trigger existed send none.
        body = await request.get_json(silent=True)
        trigger = body.get("trigger") if isinstance(body, dict) else None
        if trigger not in _HANGUP_TRIGGERS:
            trigger = "unknown"
        logger.info(f"web call {call_id}: hangup trigger={trigger}")
        ticket = await _ticket_of(call_id, record)
        if trigger == "button":
            # Written before the legs close: the relay's end-of-call report follows and must find it. A call ended by
            # the button is never resumed (VOICE_CALL_CONTROL_CHANNEL_RFC §6.2).
            await ephemeral_store.set(button_end_key(digest_key, ticket), {"button": True}, ttl_s=BUTTON_END_TTL_S)
        await _close_media(record, ticket)
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
