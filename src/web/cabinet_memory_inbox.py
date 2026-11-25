"""The Cabinet's memory-inbox routes (MCP_MEMORY_SUBMISSIONS_RFC §8.2), behind the Cabinet gate (`auth_required` sets
`g.user_id`) and the facts PIN (`facts_unlock_required`), as every facts route.

- `GET /api/user/memory-inbox`: the user's waiting candidates, newest first, each with what the card shows (its four
  editable fields, its sources, the draft classification, the relation to memory, the raw submissions) and `confirmed`,
  how many confirmed ones wait for the final pass.
- `POST /api/user/memory-inbox/<candidate_id>/confirm`: the owner's "True"; the body carries any of `text`,
  `happens_when`, `said_when`, `basis` the owner edited. 200 `{status: "confirmed"}`; 400 `{error}` (`too_long`, `empty_text`,
  `basis`, or the name of a field that is not text); 403 `other_user`; 404 `not_found`; 409 `not_waiting`.
- `POST /api/user/memory-inbox/<candidate_id>/reject`: the owner's "Not true". 200 `{status: "rejected"}`; 403; 404; 409.

- `GET`/`PUT /api/user/memory-inbox/settings`: how often the daily sweep may run the triage for the owner,
  `{triage_interval_days, choices}`; the PUT takes one of the choices (1, 2, 3, 7) or answers 400 `invalid_interval`, and
  takes effect at the next sweep.
- `GET /api/user/memory-inbox/apps`: the apps holding a live `memory_submit` grant, `{apps: [{grant_id, client_name,
  granted_at, last_refresh}]}`; `DELETE /api/user/memory-inbox/apps/<grant_id>` revokes one (404 `not_found` unless it is
  one of those; the connector's next call is refused at once).

An answered route never answers 401 (the gate does, `src/web/CLAUDE.md`). Without the inbox (no task queue) the routes
answer 501. The service logs each answer (ids and counts, never a candidate's text).
"""
from typing import TYPE_CHECKING, Any, Callable, Dict, Optional

from quart import Blueprint, g, jsonify, request

from ..domain.mcp_resource import MCPResource
from ..domain.memory_inbox import AnswerResult, Candidate, SourceCopy, triage_interval
from ..utils.logger import logger

if TYPE_CHECKING:
    from ..domain.mcp_worker_grant import WorkerGrant
    from ..ports.user_repository import UserRepository
    from ..services.mcp_authorization_service import MCPAuthorizationService
    from ..services.memory_inbox_service import MemoryInboxService

TRIAGE_INTERVAL_CHOICES = (1, 2, 3, 7)

_REFUSED = {
    AnswerResult.OTHER_USER: (403, "other_user"),
    AnswerResult.NOT_FOUND: (404, "not_found"),
    AnswerResult.NOT_WAITING: (409, "not_waiting"),
}


def register_memory_inbox_routes(bp: Blueprint, auth_required: Callable, facts_unlock_required: Callable,
                                 inbox: Optional["MemoryInboxService"],
                                 user_repo: Optional["UserRepository"] = None,
                                 mcp_authorization: Optional["MCPAuthorizationService"] = None) -> None:
    """Add the memory-inbox routes to `bp`. `user_repo` serves the settings routes, `mcp_authorization` the connected
    apps; without the latter the apps list is empty and a revoke answers 404."""

    @bp.route("/api/user/memory-inbox", methods=["GET"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_list():
        if inbox is None:
            return jsonify({"error": "Memory inbox not configured"}), 501
        try:
            view = await inbox.waiting(g.user_id)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] listing for user {g.user_id[:8]} failed: {type(exc).__name__}: {exc}",
                         exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        return jsonify({"candidates": [_card(c) for c in view.candidates], "confirmed": view.confirmed}), 200

    @bp.route("/api/user/memory-inbox/<candidate_id>/confirm", methods=["POST"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_confirm(candidate_id: str):
        if inbox is None:
            return jsonify({"error": "Memory inbox not configured"}), 501
        body = await request.get_json(silent=True)
        if body is None:
            body = {}
        if not isinstance(body, dict):
            return jsonify({"error": "body"}), 400
        try:
            answer = await inbox.confirm(g.user_id, candidate_id, body)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] confirm of {candidate_id} by user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        if answer.result is AnswerResult.INVALID:
            return jsonify({"error": answer.reason or "invalid"}), 400
        if answer.result in _REFUSED:
            status, error = _REFUSED[answer.result]
            return jsonify({"error": error}), status
        return jsonify({"status": "confirmed"}), 200

    @bp.route("/api/user/memory-inbox/<candidate_id>/reject", methods=["POST"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_reject(candidate_id: str):
        if inbox is None:
            return jsonify({"error": "Memory inbox not configured"}), 501
        try:
            answer = await inbox.reject(g.user_id, candidate_id)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] reject of {candidate_id} by user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        if answer.result in _REFUSED:
            status, error = _REFUSED[answer.result]
            return jsonify({"error": error}), status
        return jsonify({"status": "rejected"}), 200

    @bp.route("/api/user/memory-inbox/settings", methods=["GET"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_settings_get():
        if user_repo is None:
            return jsonify({"error": "Memory inbox not configured"}), 501
        try:
            user = await user_repo.get_user(g.user_id)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] settings read for user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        if user is None:
            return jsonify({"error": "User not found"}), 404
        return jsonify(_settings(user.config)), 200

    @bp.route("/api/user/memory-inbox/settings", methods=["PUT"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_settings_put():
        if user_repo is None:
            return jsonify({"error": "Memory inbox not configured"}), 501
        body = await request.get_json(force=True, silent=True)
        days = body.get("triage_interval_days") if isinstance(body, dict) else None
        if isinstance(days, bool) or not isinstance(days, int) or days not in TRIAGE_INTERVAL_CHOICES:
            return jsonify({"error": "invalid_interval"}), 400
        try:
            user = await user_repo.get_user(g.user_id)
            if user is None:
                return jsonify({"error": "User not found"}), 404
            user.config.memory_inbox_triage_interval_days = days
            await user_repo.update_user_fields(user, ("config.memory_inbox_triage_interval_days",))
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] settings write for user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        logger.info(f"[MemoryInbox] triage interval of user {g.user_id[:8]} set to {days} day(s)")
        return jsonify(_settings(user.config)), 200

    @bp.route("/api/user/memory-inbox/apps", methods=["GET"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_apps():
        if mcp_authorization is None:
            return jsonify({"apps": []}), 200
        try:
            grants = await mcp_authorization.resource_grants(g.user_id, MCPResource.MEMORY_INBOX)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] apps listing for user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        return jsonify({"apps": [_app(grant) for grant in grants]}), 200

    @bp.route("/api/user/memory-inbox/apps/<grant_id>", methods=["DELETE"])
    @auth_required
    @facts_unlock_required
    async def memory_inbox_app_revoke(grant_id: str):
        if mcp_authorization is None:
            return jsonify({"error": "not_found"}), 404
        try:
            grants = await mcp_authorization.resource_grants(g.user_id, MCPResource.MEMORY_INBOX)
            if not any(grant.grant_id == grant_id for grant in grants):
                return jsonify({"error": "not_found"}), 404
            await mcp_authorization.revoke_grant(g.user_id, grant_id)
        except Exception as exc:
            logger.error(f"❌ [MemoryInbox] app revoke by user {g.user_id[:8]} failed: "
                         f"{type(exc).__name__}: {exc}", exc_info=True)
            return jsonify({"error": "Internal server error"}), 500
        logger.info(f"[MemoryInbox] user {g.user_id[:8]} revoked a connected app")
        return jsonify({"revoked": True}), 200


def _settings(config: Any) -> Dict[str, Any]:
    """The effective triage interval (the account default is not read here) and the choices the Cabinet offers."""
    return {"triage_interval_days": triage_interval(config, None), "choices": list(TRIAGE_INTERVAL_CHOICES)}


def _app(grant: "WorkerGrant") -> Dict[str, Any]:
    return {"grant_id": grant.grant_id, "client_name": grant.client_name,
            "granted_at": grant.granted_at.isoformat() if grant.granted_at else None,
            "last_refresh": grant.last_refresh.isoformat() if grant.last_refresh else None}


def _card(candidate: Candidate) -> Dict[str, Any]:
    """One candidate as the Cabinet's card reads it. `source` names the assistants its submissions came from (in the
    order they arrived, each once); `received` is when the newest arrived."""
    names = list(dict.fromkeys(s.client_name for s in sorted(candidate.sources, key=lambda s: s.received_at)))
    return {
        "candidate_id": candidate.candidate_id,
        "text": candidate.text,
        "happens_when": candidate.happens_when,
        "said_when": candidate.said_when,
        "basis": candidate.basis.value,
        "source": ", ".join(names),
        "received": candidate.received.isoformat(),
        "draft": {"directive": candidate.draft.directive, "temporal_class": candidate.draft.temporal_class,
                  "context_priority": candidate.draft.context_priority, "expiry": dict(candidate.draft.expiry)},
        "relation": {"kind": candidate.relation.kind.value, "fact_text": candidate.relation.fact_text},
        "raw": [_raw(s) for s in candidate.sources],
    }


def _raw(source: SourceCopy) -> Dict[str, Any]:
    return {"information": source.information, "basis": source.basis.value, "said_when": source.said_when,
            "happens_when": source.happens_when, "received": source.received_at.isoformat()}
