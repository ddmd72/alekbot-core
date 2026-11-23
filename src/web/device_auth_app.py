"""The Cabinet's devices list and its logouts (CABINET_DEVICE_SESSIONS_RFC §7.5, §7.6), every route behind the Cabinet gate.

- `GET  /api/user/devices` — the caller's live sessions: label, platform hint, sign-in kind, created, last used and
  `is_current` (the session the request's own token names; a token without `sid` marks no row).
- `POST /api/user/devices/<sid>/logout` — one row's "Log out"; another user's sid is refused like an unknown one (404).
- `POST /api/user/devices/logout-others` — every live session but the current one; refused (400) when the token names
  no session, since "the others" is then unknown.
- `POST /api/user/devices/logout-all` — the kill switch (`revoke_all`, reason `kill_switch`), this device included.

`label` and `platform_hint` are User-Agent text: untrusted. They travel in JSON as data, and the page escapes them.
"""
from typing import Optional

from quart import Blueprint, g, jsonify

from ..domain.device_session import DeviceSession, SessionRevokeReason
from ..services.device_session_service import DeviceSessionService
from ..utils.logger import logger
from .cabinet_auth import CabinetSessionVerifier, make_auth_required


def _row(session: DeviceSession, current_sid: Optional[str]) -> dict:
    return {
        "sid": session.sid,
        "label": session.label,
        "platform_hint": session.platform_hint,
        "auth_kind": session.auth_kind.value,
        "created_at": session.created_at.isoformat(),
        "last_seen_at": session.last_seen_at.isoformat(),
        "is_current": current_sid is not None and session.sid == current_sid,
    }


def create_device_auth_blueprint(verifier: CabinetSessionVerifier, device_sessions: DeviceSessionService) -> Blueprint:
    bp = Blueprint("device_auth", __name__)
    auth_required = make_auth_required(verifier)

    def _current_sid() -> Optional[str]:
        """The `sid` of the verified, live token the gate just accepted (`g.session_id`)."""
        sid = getattr(g, "session_id", None)
        return sid if isinstance(sid, str) and sid else None

    @bp.route("/api/user/devices", methods=["GET"])
    @auth_required
    async def list_devices():
        current = _current_sid()
        sessions = await device_sessions.list_for_user(g.user_id)
        return jsonify({"devices": [_row(s, current) for s in sessions]}), 200

    @bp.route("/api/user/devices/<sid>/logout", methods=["POST"])
    @auth_required
    async def logout_device(sid: str):
        if not await device_sessions.revoke(g.user_id, sid, SessionRevokeReason.DEVICE_LIST):
            return jsonify({"error": "No such device"}), 404
        return jsonify({"revoked": 1, "current": sid == _current_sid()}), 200

    @bp.route("/api/user/devices/logout-others", methods=["POST"])
    @auth_required
    async def logout_others():
        current = _current_sid()
        revoked = 0
        for session in await device_sessions.list_for_user(g.user_id):
            if session.sid != current and await device_sessions.revoke(
                    g.user_id, session.sid, SessionRevokeReason.DEVICE_LIST):
                revoked += 1
        return jsonify({"revoked": revoked}), 200

    @bp.route("/api/user/devices/logout-all", methods=["POST"])
    @auth_required
    async def logout_all():
        revoked = await device_sessions.revoke_all(g.user_id, SessionRevokeReason.KILL_SWITCH)
        logger.warning(f"⛔ Cabinet kill switch: user={g.user_id[:8]} revoked={revoked}")
        return jsonify({"revoked": revoked}), 200

    return bp
