"""The Cabinet's facts PIN routes and the guard of the facts routes (CABINET_DEVICE_SESSIONS_RFC §7.7).

Behind the Cabinet gate (`make_auth_required`; it sets `g.user_id`, `g.account_id`, `g.session_id`):
- `GET  /api/user/facts-pin` — `{has_pin, unlocked, reset_due_at, can_request_reset}`: what the page needs to decide
  between the plain facts tab, the PIN prompt and the settings card. Never a code or a digest.
- `POST /api/user/facts-pin/set` — `{pin, pin_confirm, duress, duress_confirm}`: a first PIN and duress code, each
  typed twice (a mismatch is 400 `mismatch`; a rule the codes break is 400 with the `CodeProblem` as `error`).
- `POST /api/user/facts-pin/change` — `{current, pin, pin_confirm, duress, duress_confirm}`; `POST .../remove` —
  `{current}`. Both check `current` as an attempt: a wrong one counts.
- `POST /api/user/facts-pin/unlock` — `{pin}`: opens the facts tab on this session for `FactsPinPolicy.UNLOCK_TTL`.
- `POST /api/user/facts-pin/relock` — the `sendBeacon` target (leaving the tab or closing the page); always 204.
- `POST /api/user/facts-pin/reset` and `.../reset/cancel` — a forgotten PIN: the 24 h reset needs a Google sign-in under
  five minutes old on this session (403 `reset_refused` otherwise).

Answers never carry a code. A wrong code is 400 `wrong_code` with `attempts_left`; the duress code and the fifth wrong
code in a row are both 423 `account_locked` (the same answer, so the page cannot tell them apart). A refusal is never
401: `authFetch` answers a 401 by refreshing and re-sending (src/web/CLAUDE.md). Nothing here logs a request body.

`make_facts_unlock_required` builds the guard the facts routes of the Cabinet carry (`facts_unlock_required`): no PIN
set (or no service wired) passes; a PIN and a session not unlocked answers 403 `facts_locked`, and a failed read
answers the same (closed).
"""
from functools import wraps
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from quart import Blueprint, Response, g, jsonify, request

from ..domain.facts_pin_policy import CodeProblem, PinChangeStatus, PinOutcome, PinVerdict
from ..utils.logger import logger
from .cabinet_auth import CabinetSessionVerifier, make_auth_required

if TYPE_CHECKING:
    from ..services.device_session_service import DeviceSessionService
    from ..services.facts_pin_service import FactsPinService

GUARD_MARK = "facts_guarded"


def make_facts_unlock_required(facts_pin: Optional["FactsPinService"]) -> Callable:
    """The `facts_unlock_required` decorator. It sits under `auth_required` (it reads `g.session_id`)."""

    def facts_unlock_required(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            if facts_pin is not None:
                try:
                    is_open = await facts_pin.facts_open(g.user_id, g.account_id, g.session_id)
                except Exception as exc:
                    logger.error(f"💥 Facts PIN guard failed, request refused: user={g.user_id[:8]}: {exc}",
                                 exc_info=True)
                    is_open = False
                if not is_open:
                    return jsonify({"error": "facts_locked"}), 403
            return await func(*args, **kwargs)
        setattr(wrapper, GUARD_MARK, True)
        return wrapper

    return facts_unlock_required


def _body_strings(body: Any, *names: str) -> Optional[Dict[str, str]]:
    """The named fields of a JSON object body when every one is a string; None for any other body."""
    if not isinstance(body, dict) or not all(isinstance(body.get(name), str) for name in names):
        return None
    return {name: body[name] for name in names}


def _bad(error: str, status: int = 400, **extra: Any):
    return jsonify({"error": error, **extra}), status


def _account_locked():
    return jsonify({"error": "account_locked"}), 423


def _refusal(attempt: Optional[PinOutcome]):
    """The answer to a code that was not the PIN: a wrong one reports the attempts left, a lock is 423."""
    if attempt is not None and attempt.verdict is PinVerdict.WRONG:
        return _bad("wrong_code", attempts_left=attempt.attempts_left)
    return _account_locked()


def create_facts_pin_blueprint(verifier: CabinetSessionVerifier, facts_pin: "FactsPinService",
                               device_sessions: "DeviceSessionService") -> Blueprint:
    """The facts PIN routes over `facts_pin`; `device_sessions` loads the caller's own session for a reset request."""
    bp = Blueprint("facts_pin", __name__)
    auth_required = make_auth_required(verifier)

    async def _new_codes(*fields: str) -> Optional[Dict[str, str]]:
        """`fields` plus the two new codes, each typed twice; the refusal answer is raised as `_Refused`."""
        values = _body_strings(await request.get_json(silent=True), *fields, "pin", "pin_confirm", "duress",
                               "duress_confirm")
        if values is None:
            raise _Refused(_bad("invalid_body"))
        if values["pin"] != values["pin_confirm"] or values["duress"] != values["duress_confirm"]:
            raise _Refused(_bad("mismatch"))
        return values

    def _changed(status: PinChangeStatus, problem: Optional[CodeProblem], attempt: Optional[PinOutcome]):
        if status is PinChangeStatus.DONE:
            return jsonify({"status": "done"}), 200
        if status is PinChangeStatus.CODES_REFUSED:
            return _bad(problem.value if problem else "codes_refused")
        if status is PinChangeStatus.ALREADY_SET:
            return _bad("already_set", 409)
        if status is PinChangeStatus.NO_PIN:
            return _bad("no_pin", 409)
        return _refusal(attempt)

    @bp.route("/api/user/facts-pin", methods=["GET"])
    @auth_required
    async def pin_status():
        record = await facts_pin.status(g.user_id, g.account_id)
        session = await device_sessions.current(g.user_id, g.session_id)
        unlocked = record is not None and await facts_pin.facts_open(g.user_id, g.account_id, g.session_id)
        return jsonify({
            "has_pin": record is not None,
            "unlocked": unlocked,
            "reset_due_at": record.reset_due_at.isoformat() if record and record.reset_due_at else None,
            "can_request_reset": bool(record and session and facts_pin.can_request_reset(session)),
        }), 200

    @bp.route("/api/user/facts-pin/set", methods=["POST"])
    @auth_required
    async def pin_set():
        try:
            values = await _new_codes()
        except _Refused as refused:
            return refused.answer
        result = await facts_pin.set(g.user_id, values["pin"], values["duress"])
        return _changed(result.status, result.problem, result.attempt)

    @bp.route("/api/user/facts-pin/change", methods=["POST"])
    @auth_required
    async def pin_change():
        try:
            values = await _new_codes("current")
        except _Refused as refused:
            return refused.answer
        result = await facts_pin.change(g.user_id, g.account_id, values["current"], values["pin"], values["duress"])
        return _changed(result.status, result.problem, result.attempt)

    @bp.route("/api/user/facts-pin/remove", methods=["POST"])
    @auth_required
    async def pin_remove():
        values = _body_strings(await request.get_json(silent=True), "current")
        if values is None:
            return _bad("invalid_body")
        result = await facts_pin.remove(g.user_id, g.account_id, values["current"])
        return _changed(result.status, result.problem, result.attempt)

    @bp.route("/api/user/facts-pin/unlock", methods=["POST"])
    @auth_required
    async def pin_unlock():
        values = _body_strings(await request.get_json(silent=True), "pin")
        if values is None:
            return _bad("invalid_body")
        try:
            attempt, unlocked = await facts_pin.unlock_session(g.user_id, g.account_id, g.session_id, values["pin"])
        except PermissionError:
            return _bad("session_ended", 403)
        if attempt.verdict is PinVerdict.NO_PIN:
            return _bad("no_pin", 409)
        if attempt.verdict is not PinVerdict.OK:
            return _refusal(attempt)
        if not unlocked:
            return _bad("session_ended", 403)
        return jsonify({"status": "unlocked"}), 200

    @bp.route("/api/user/facts-pin/relock", methods=["POST"])
    @auth_required
    async def pin_relock():
        await facts_pin.relock_session(g.user_id, g.session_id)
        return Response(status=204)

    @bp.route("/api/user/facts-pin/reset", methods=["POST"])
    @auth_required
    async def pin_reset_request():
        session = await device_sessions.current(g.user_id, g.session_id)
        due = await facts_pin.request_reset(session) if session is not None else None
        if due is None:
            return _bad("reset_refused", 403)
        return jsonify({"status": "pending", "reset_due_at": due.isoformat()}), 200

    @bp.route("/api/user/facts-pin/reset/cancel", methods=["POST"])
    @auth_required
    async def pin_reset_cancel():
        if not await facts_pin.cancel_reset(g.user_id):
            return _bad("no_reset_pending", 409)
        return jsonify({"status": "cancelled"}), 200

    return bp


class _Refused(Exception):
    """A request body refused before any service call; `answer` is the response."""

    def __init__(self, answer: Any) -> None:
        super().__init__("refused")
        self.answer = answer
