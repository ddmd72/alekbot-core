"""The Cabinet's devices list, its logouts and the passkey routes (CABINET_DEVICE_SESSIONS_RFC §7.3, §7.5, §7.6).

Behind the Cabinet gate:
- `GET  /api/user/devices` — the caller's live sessions: label, platform hint, sign-in kind, created, last used and
  `is_current` (the session the request's own token names; a token without `sid` marks no row).
- `POST /api/user/devices/<sid>/logout` — one row's "Log out"; another user's sid is refused like an unknown one (404).
- `POST /api/user/devices/logout-others` — every live session but the current one; refused (400) when the token names
  no session, since "the others" is then unknown.
- `POST /api/user/devices/logout-all` — the kill switch (`revoke_all`, reason `kill_switch`), this device included.
- `GET  /api/user/passkeys` — the caller's live passkeys (listed under the sessions, each with "Remove") and `offer`:
  whether this session was signed in with Google, so the page may offer "Add Face ID" once (it remembers a dismissal
  per browser).
- `POST /api/user/passkeys/register/options` and `/register/verify` — enrolment from this session ("Add Face ID for
  faster sign-in"); the user is loaded with `resume_session`, so an inactive or de-listed user cannot add one.
- `POST /api/user/passkeys/<cred_hash>/remove` — revoke one of the caller's passkeys; another user's is 404.
- `POST /api/user/passkeys/step-up/options` and `/step-up/verify` — the passkey a voice call start needs (§7.3, §13
  decision 1): `options` answers `{"required": false}` when the call may start as it is, else the assertion options;
  `verify` records the assertion on this session.

Anonymous by design (the sign-in page runs them before any session exists):
- `POST /auth/passkey/options` — a sign-in challenge for any passkey of this relying party.
- `POST /auth/passkey/verify` — the assertion; on success the same stored-identity checks as a refresh
  (`resume_session`: active user, whitelisted email, existing account), a new `passkey` session, the session cookies
  as the Google callback sets them, and `{"next": <safe path>}` for the page to go to.

Each ceremony's nonce travels in its own short-lived `__Host-` cookie (HttpOnly, SameSite=Strict, the challenge's TTL),
so a sign-in, an enrolment and a step-up running in parallel tabs do not overwrite each other. A refusal of a gated
ceremony is 400, never 401: `authFetch` answers a 401 by refreshing and re-sending, and a re-sent nonce is spent.
Without the passkey service every passkey route answers 501.

`label` and `platform_hint` are User-Agent text: untrusted. They travel in JSON as data, and the page escapes them.
"""
from typing import TYPE_CHECKING, Any, Dict, Optional

from quart import Blueprint, g, jsonify, make_response, request

from ..domain.device_label import describe_device
from ..domain.device_session import DeviceSession, SessionAuthKind, SessionRevokeReason
from ..domain.exceptions import AccessDeniedError
from ..domain.passkey import Passkey
from ..domain.passkey_ceremony import PasskeyRejected
from ..domain.safe_redirect import safe_return_path
from ..services.device_session_service import DeviceSessionService
from ..utils.logger import logger
from .cabinet_auth import CabinetSessionVerifier, make_auth_required, set_session_cookies

if TYPE_CHECKING:
    from ..services.authentication_service import AuthenticationService
    from ..services.passkey_service import PasskeyService

SIGN_IN_COOKIE = "__Host-passkey-sign-in"
REGISTER_COOKIE = "__Host-passkey-register"
STEP_UP_COOKIE = "__Host-passkey-step-up"
_NONCE_FLAGS: Dict[str, Any] = {"secure": True, "httponly": True, "samesite": "Strict", "path": "/"}


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


def _passkey_row(passkey: Passkey) -> dict:
    return {
        "id": passkey.cred_hash,
        "label": passkey.label,
        "platform_hint": passkey.platform_hint,
        "synced": passkey.backup_eligible,
        "created_at": passkey.created_at.isoformat(),
        "last_used_at": passkey.last_used_at.isoformat() if passkey.last_used_at else None,
    }


async def _credential() -> Optional[Dict[str, Any]]:
    """The browser's credential from a `{"credential": {...}}` JSON body; None for any other body."""
    body = await request.get_json(silent=True)
    credential = body.get("credential") if isinstance(body, dict) else None
    return credential if isinstance(credential, dict) else None


async def _with_nonce(body: Dict[str, Any], cookie: str, nonce: str, ttl_s: int):
    response = await make_response(jsonify(body), 200)
    response.set_cookie(cookie, nonce, max_age=ttl_s, **_NONCE_FLAGS)
    return response


def _spend(response: Any, cookie: str) -> None:
    response.delete_cookie(cookie, **_NONCE_FLAGS)


def create_device_auth_blueprint(verifier: CabinetSessionVerifier, device_sessions: DeviceSessionService,
                                 passkeys: Optional["PasskeyService"] = None,
                                 auth_service: Optional["AuthenticationService"] = None) -> Blueprint:
    """The devices routes over `device_sessions`; the passkey routes over `passkeys` and `auth_service` (both or
    neither: without them every passkey route answers 501)."""
    bp = Blueprint("device_auth", __name__)
    auth_required = make_auth_required(verifier)

    def _current_sid() -> Optional[str]:
        """The `sid` of the verified, live token the gate just accepted (`g.session_id`)."""
        sid = getattr(g, "session_id", None)
        return sid if isinstance(sid, str) and sid else None

    def _unconfigured():
        return jsonify({"error": "Passkeys not configured"}), 501

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

    # ------------------------------------------------------------------ passkeys of the signed-in user

    @bp.route("/api/user/passkeys", methods=["GET"])
    @auth_required
    async def list_passkeys():
        if passkeys is None:
            return _unconfigured()
        session = await device_sessions.current(g.user_id, g.session_id)
        live = await passkeys.live_passkeys(g.user_id)
        offer = session is not None and session.auth_kind is SessionAuthKind.GOOGLE
        return jsonify({"passkeys": [_passkey_row(p) for p in live], "offer": offer}), 200

    @bp.route("/api/user/passkeys/register/options", methods=["POST"])
    @auth_required
    async def register_options():
        if passkeys is None or auth_service is None:
            return _unconfigured()
        session = await device_sessions.current(g.user_id, g.session_id)
        if session is None:
            return jsonify({"error": "Session ended"}), 400
        try:
            user, _ = await auth_service.resume_session(g.user_id)
        except AccessDeniedError:
            return jsonify({"error": "Access not authorized"}), 403
        start = await passkeys.begin_registration(user, session)
        return await _with_nonce({"options": start.options}, REGISTER_COOKIE, start.nonce, passkeys.CHALLENGE_TTL_S)

    @bp.route("/api/user/passkeys/register/verify", methods=["POST"])
    @auth_required
    async def register_verify():
        if passkeys is None or auth_service is None:
            return _unconfigured()
        nonce, credential = request.cookies.get(REGISTER_COOKIE), await _credential()
        if not nonce or credential is None:
            return jsonify({"error": "No passkey ceremony to finish"}), 400
        session = await device_sessions.current(g.user_id, g.session_id)
        if session is None:
            return jsonify({"error": "Session ended"}), 400
        try:
            user, _ = await auth_service.resume_session(g.user_id)
        except AccessDeniedError:
            return jsonify({"error": "Access not authorized"}), 403
        try:
            passkey = await passkeys.finish_registration(nonce, credential, user, session)
        except PasskeyRejected:
            response = await make_response(jsonify({"error": "passkey_refused"}), 400)
        else:
            response = await make_response(jsonify({"passkey": _passkey_row(passkey)}), 201)
        _spend(response, REGISTER_COOKIE)
        return response

    @bp.route("/api/user/passkeys/<cred_hash>/remove", methods=["POST"])
    @auth_required
    async def remove_passkey(cred_hash: str):
        if passkeys is None:
            return _unconfigured()
        if not await passkeys.remove(g.user_id, cred_hash):
            return jsonify({"error": "No such passkey"}), 404
        return jsonify({"removed": 1}), 200

    @bp.route("/api/user/passkeys/step-up/options", methods=["POST"])
    @auth_required
    async def step_up_options():
        if passkeys is None:
            return _unconfigured()
        if await passkeys.call_start_allowed(g.user_id, g.session_id):
            return jsonify({"required": False}), 200
        start = await passkeys.begin_step_up(g.user_id, g.session_id)
        return await _with_nonce({"required": True, "options": start.options}, STEP_UP_COOKIE, start.nonce,
                                 passkeys.CHALLENGE_TTL_S)

    @bp.route("/api/user/passkeys/step-up/verify", methods=["POST"])
    @auth_required
    async def step_up_verify():
        if passkeys is None:
            return _unconfigured()
        nonce, credential = request.cookies.get(STEP_UP_COOKIE), await _credential()
        if not nonce or credential is None:
            return jsonify({"error": "No passkey ceremony to finish"}), 400
        try:
            await passkeys.finish_step_up(nonce, credential, g.user_id, g.session_id)
        except PasskeyRejected:
            response = await make_response(jsonify({"error": "passkey_refused"}), 400)
        else:
            response = await make_response(jsonify({"ok": True}), 200)
        _spend(response, STEP_UP_COOKIE)
        return response

    # ------------------------------------------------------------------ passkey sign-in (anonymous)

    @bp.route("/auth/passkey/options", methods=["POST"])
    async def sign_in_options():
        if passkeys is None or auth_service is None:
            return _unconfigured()
        start = await passkeys.begin_sign_in()
        return await _with_nonce({"options": start.options}, SIGN_IN_COOKIE, start.nonce, passkeys.CHALLENGE_TTL_S)

    @bp.route("/auth/passkey/verify", methods=["POST"])
    async def sign_in_verify():
        if passkeys is None or auth_service is None:
            return _unconfigured()
        body = await request.get_json(silent=True)
        nonce = request.cookies.get(SIGN_IN_COOKIE)
        credential = body.get("credential") if isinstance(body, dict) else None
        if not nonce or not isinstance(credential, dict):
            return jsonify({"error": "No passkey ceremony to finish"}), 400
        next_url = safe_return_path(body.get("next"))
        try:
            user_id = await passkeys.finish_sign_in(nonce, credential)
            user, account = await auth_service.resume_session(user_id)
        except PasskeyRejected:
            response = await make_response(jsonify({"error": "passkey_refused"}), 401)
        except AccessDeniedError:
            # The same refusal as a refresh of this user: inactive, de-listed, or without an account (logged there).
            response = await make_response(jsonify({"error": "access_denied"}), 403)
        else:
            label, platform_hint = describe_device(request.headers.get("User-Agent", ""), next_url)
            session, access_token, refresh_secret = await device_sessions.start(
                user, account, label=label, platform_hint=platform_hint, auth_kind=SessionAuthKind.PASSKEY)
            logger.info(f"✅ Passkey sign-in success - user: {user.user_id}, sid: {session.sid[:8]}")
            response = await make_response(jsonify({"next": next_url}), 200)
            set_session_cookies(response, session, access_token, refresh_secret)
        _spend(response, SIGN_IN_COOKIE)
        return response

    return bp
