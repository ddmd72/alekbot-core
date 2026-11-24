"""The Cabinet's External workers routes (EXTERNAL_TASK_QUEUE_RFC §5.6, §5.10), behind the Cabinet gate (`auth_required`
sets `g.user_id`). Register and remove a worker, mint and revoke its keys, set and clear its wake-up target. No edit of
label or description, no grants, no drive destination.

- `GET    /api/external-workers` — `{enabled, workers: [{worker_id, label, description, created_at, wake_host,
  keys: [{key_id, hint, created_at, last_used_at}]}]}`: active workers and their active keys. `wake_host` is the
  target's host, or null (the worker polls); the URL and the headers are never sent back. A user without the flag, or a
  server without the service, gets `{enabled: false}` (200): the page hides the row.
- `POST   /api/external-workers` `{label, description}` — the new worker (same shape, no keys).
- `DELETE /api/external-workers/<worker_id>` — `{cancelled}`: how many non-final tasks the removal cancelled.
- `POST   /api/external-workers/<worker_id>/keys` — `{key, key_id, hint}`: the raw key is in this one answer only.
- `DELETE /api/external-workers/<worker_id>/keys/<key_id>` — revokes one key.
- `PUT    /api/external-workers/<worker_id>/wake` `{curl, secret}` or `{url, headers}` — `{wake_host}`. `curl` is the
  pasted command (`src/domain/wake_curl.py`), `secret` goes where it has a `$NAME` placeholder (optional, may be empty);
  `headers` is optional text, one `Name: value` per line. A body of both shapes or of neither is 400 `invalid_body`.
  The whole target is replaced (§5.6). A target the guard refuses is 400 with `error` `wake_<code>` (the closed
  `WakeRefusal` code; `wake_header` also for a line that is not `Name: value`; `wake_curl_*` when the command could not
  be read). The command and the secret are never logged or sent back.
- `DELETE /api/external-workers/<worker_id>/wake` — `{wake_host: null}`: the worker polls from now on.

`key_id` is `fingerprint(key_digest)`: the page names a key by it and the server resolves it among that worker's own
keys, so the digest never leaves the server. Refusals: `account_locked` is 423; a mutation while the flag is off,
another user's worker and an unknown key are one 404 `not_found`; `label_invalid`, `label_taken`, `workers_cap`,
`keys_cap` and `wake_unavailable` are 400 with the code as `error`. A refusal is never 401: `authFetch` answers a 401 by
refreshing and re-sending (src/web/CLAUDE.md). Nothing here logs a key, a description, a wake-up URL or a header.
"""
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from quart import Blueprint, g, jsonify, request

from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_worker import ExternalWorker
from ..domain.external_worker_key import ExternalWorkerKey
from ..domain.secret_digest import fingerprint
from ..domain.wake_refusal import WakeRefusal, WakeTargetRefused
from ..domain.wake_curl import parse_wake_curl
from ..domain.wake_target import Headers, parse_header_lines

if TYPE_CHECKING:
    from ..services.external_task_service import ExternalTaskService

_BAD_REQUEST = {TaskRefusal.LABEL_INVALID, TaskRefusal.LABEL_TAKEN, TaskRefusal.WORKERS_CAP, TaskRefusal.KEYS_CAP,
                TaskRefusal.WAKE_UNAVAILABLE}


def _key_view(key: ExternalWorkerKey) -> Dict[str, Any]:
    return {"key_id": fingerprint(key.key_digest), "hint": key.hint, "created_at": key.created_at.isoformat(),
            "last_used_at": key.last_used_at.isoformat() if key.last_used_at else None}


def _worker_view(worker: ExternalWorker, keys: List[ExternalWorkerKey]) -> Dict[str, Any]:
    return {"worker_id": worker.worker_id, "label": worker.label, "description": worker.description,
            "created_at": worker.created_at.isoformat(), "wake_host": worker.wake_host if worker.has_wake else None,
            "keys": [_key_view(k) for k in keys if not k.is_revoked]}


def _refusal(reason: TaskRefusal):
    if reason is TaskRefusal.ACCOUNT_LOCKED:
        return jsonify({"error": "account_locked"}), 423
    if reason in _BAD_REQUEST:
        return jsonify({"error": reason.value}), 400
    return jsonify({"error": "not_found"}), 404


def _wake_target_of(body: Any) -> "Optional[Union[Tuple[str, Headers], WakeRefusal]]":
    """The (url, headers) of a wake-up body, a refusal, or None for a body of neither shape or of both: `{curl, secret}`
    (the pasted command and its secret) or the older `{url, headers}`."""
    if not isinstance(body, dict):
        return None
    if "curl" in body:
        secret = body.get("secret", "")
        if "url" in body or "headers" in body or not isinstance(body["curl"], str) \
                or not isinstance(secret, (str, type(None))):
            return None
        return parse_wake_curl(body["curl"], secret or "")
    lines = body.get("headers", "")
    if "secret" in body or not isinstance(body.get("url"), str) or not isinstance(lines, (str, type(None))):
        return None
    headers = parse_header_lines(lines or "")
    return headers if isinstance(headers, WakeRefusal) else (body["url"], headers)


def _wake_refusal(refusal: WakeRefusal):
    return jsonify({"error": f"wake_{refusal.value}"}), 400


def register_external_worker_routes(bp: Blueprint, auth_required: Callable,
                                    external_tasks: Optional["ExternalTaskService"]) -> None:
    """Add the routes to `bp`. Registered even without the service: they then answer as a user without the flag."""

    @bp.route("/api/external-workers", methods=["GET"])
    @auth_required
    async def external_workers_list():
        if external_tasks is None:
            return jsonify({"enabled": False}), 200
        try:
            workers = await external_tasks.list_workers(g.user_id)
            views = [_worker_view(w, await external_tasks.list_keys(g.user_id, w.worker_id)) for w in workers]
        except ExternalTaskRefused as refused:
            if refused.reason is TaskRefusal.NOT_ENABLED:
                return jsonify({"enabled": False}), 200
            return _refusal(refused.reason)
        return jsonify({"enabled": True, "workers": views}), 200

    @bp.route("/api/external-workers", methods=["POST"])
    @auth_required
    async def external_workers_create():
        body = await request.get_json(silent=True)
        description = body.get("description") if isinstance(body, dict) else None
        if (not isinstance(body, dict) or not isinstance(body.get("label"), str)
                or not (description is None or isinstance(description, str))):
            return jsonify({"error": "invalid_body"}), 400
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            worker = await external_tasks.create_worker(g.user_id, body["label"], description)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify(_worker_view(worker, [])), 200

    @bp.route("/api/external-workers/<worker_id>", methods=["DELETE"])
    @auth_required
    async def external_workers_remove(worker_id: str):
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            cancelled = await external_tasks.revoke_worker(g.user_id, worker_id)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify({"cancelled": cancelled}), 200

    @bp.route("/api/external-workers/<worker_id>/keys", methods=["POST"])
    @auth_required
    async def external_workers_mint_key(worker_id: str):
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            minted = await external_tasks.mint_key(g.user_id, worker_id)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify({"key": minted.key, "key_id": fingerprint(minted.key_digest), "hint": minted.hint}), 200

    @bp.route("/api/external-workers/<worker_id>/keys/<key_id>", methods=["DELETE"])
    @auth_required
    async def external_workers_revoke_key(worker_id: str, key_id: str):
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            keys = await external_tasks.list_keys(g.user_id, worker_id)
            match = next((k for k in keys if not k.is_revoked and fingerprint(k.key_digest) == key_id), None)
            if match is None:
                return _refusal(TaskRefusal.NOT_FOUND)
            await external_tasks.revoke_key(g.user_id, worker_id, match.key_digest)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify({"revoked": True}), 200

    @bp.route("/api/external-workers/<worker_id>/wake", methods=["PUT"])
    @auth_required
    async def external_workers_set_wake(worker_id: str):
        body = await request.get_json(silent=True)
        parsed = _wake_target_of(body)
        if parsed is None:
            return jsonify({"error": "invalid_body"}), 400
        if isinstance(parsed, WakeRefusal):
            return _wake_refusal(parsed)
        url, headers = parsed
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            worker = await external_tasks.save_wake_target(g.user_id, worker_id, url, headers)
        except WakeTargetRefused as refused:
            return _wake_refusal(refused.refusal)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify({"wake_host": worker.wake_host}), 200

    @bp.route("/api/external-workers/<worker_id>/wake", methods=["DELETE"])
    @auth_required
    async def external_workers_clear_wake(worker_id: str):
        if external_tasks is None:
            return _refusal(TaskRefusal.NOT_ENABLED)
        try:
            await external_tasks.clear_wake_target(g.user_id, worker_id)
        except ExternalTaskRefused as refused:
            return _refusal(refused.reason)
        return jsonify({"wake_host": None}), 200
