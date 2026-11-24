"""The worker REST endpoint `/external-tasks/v1/*` (EXTERNAL_TASK_QUEUE_RFC §5.5, §5.6, §5.7).

Not under `/api/`: these routes are not behind the Cabinet cookie gate. Auth is a worker key in `Authorization: Bearer`
only. One thin handler per route over `ExternalTaskService`; answers are JSON `{"code": ...}` with no text (the
texts of `MCP_TASK_RESULT_TEXTS` are a later step).

Per request, in order:
1. `401` limits already exhausted (this address, or all addresses) -> `429` + `Retry-After`; the key store is not read;
2. `Authorization: Bearer` parsed and the key's shape checked (`has_key_shape`); a malformed key is `401` and counts;
3. `resolve_key`; `key_invalid` is `401` and counts; the service's own refusals (locked, flag off, revoked) are `403`;
4. the whitelist, which the core leaves out (`ExternalWorkerUserGate`, shared with the MCP resource): the stored user
   must still exist, have an email and be whitelisted;
5. this worker's limits: `CALLS_PER_WINDOW`, and `CLAIMS_PER_WINDOW` on claim, `SUBMITS_PER_WINDOW` on submit;
6. the body (a POST with a body must be `application/json`; capped before it is parsed) and the service call.

A task of another worker or user, a held task and a missing one are one and the same `404`. Logs name the worker by
`fingerprint(worker_id)` and the user by 8 characters; never a key, text, result or header.
"""
import json
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from quart import Blueprint, Response, request

from ..domain.external_task import (
    CALLS_PER_WINDOW,
    CLAIMS_PER_WINDOW,
    RESULT_MAX_BYTES,
    SUBMITS_PER_WINDOW,
)
from ..domain.external_task_http import client_address, status_for
from ..domain.external_task_refusal import ExternalTaskRefused, TaskRefusal
from ..domain.external_worker_key import has_key_shape
from ..domain.external_worker_principal import WorkerPrincipal
from ..domain.secret_digest import fingerprint
from ..ports.user_repository import UserRepository
from ..ports.whitelist_repository import WhitelistRepository
from ..services.external_task_rate_limiter import ExternalTaskRateLimiter
from ..services.external_task_service import ExternalTaskService
from ..services.external_worker_user_gate import ExternalWorkerUserGate
from ..utils.logger import logger

BODY_OVERHEAD_BYTES = 4_096       # the JSON envelope around a result; the service caps the text itself
BODY_CAP_BYTES = RESULT_MAX_BYTES + BODY_OVERHEAD_BYTES
WRITTEN_BY = "written by the user's assistant at the user's request"
_BEARER = "bearer "

_HEADERS = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex"}


def _json(status: int, body: Dict[str, Any], **headers: str) -> Response:
    return Response(json.dumps(body, ensure_ascii=False), status=status, mimetype="application/json",
                    headers={**_HEADERS, **headers})


def _code(status: int, code: str, **headers: str) -> Response:
    return _json(status, {"code": code}, **headers)


def _rate_limited(retry_after: int) -> Response:
    return _code(429, "rate_limited", **{"Retry-After": str(retry_after)})


def _bearer_key() -> str:
    header = request.headers.get("Authorization", "")
    return header[len(_BEARER):].strip() if header[:len(_BEARER)].lower() == _BEARER else ""


class _Early(Exception):
    """An answer decided before the service is called."""

    def __init__(self, response: Response) -> None:
        self.response = response


def create_external_task_api_blueprint(external_tasks: ExternalTaskService, *, limiter: ExternalTaskRateLimiter,
                                       user_repo: UserRepository, whitelist_repo: WhitelistRepository) -> Blueprint:
    bp = Blueprint("external_task_api", __name__, url_prefix="/external-tasks/v1")
    gate = ExternalWorkerUserGate(user_repo, whitelist_repo)

    async def unauthorized() -> Response:
        """The one `401` answer; counted, and a `429` once the count is over a limit."""
        wait = await limiter.count_unauthorized(client_address(request.headers.get("X-Forwarded-For")))
        if wait is not None:
            logger.warning("[TaskAPI] rate limited: unauthorized")
            return _rate_limited(wait)
        logger.info("[TaskAPI] unauthorized")
        return _code(401, TaskRefusal.KEY_INVALID.value)

    async def authenticate(scopes: Tuple[Tuple[str, int], ...]) -> WorkerPrincipal:
        address = client_address(request.headers.get("X-Forwarded-For"))
        wait = await limiter.unauthorized_retry_after(address)
        if wait is not None:
            raise _Early(_rate_limited(wait))
        key = _bearer_key()
        if not has_key_shape(key):
            raise _Early(await unauthorized())
        digest = external_tasks.key_digest(key)
        wait = await limiter.key_retry_after(digest, [scope for scope, _ in scopes])
        if wait is not None:     # this key tripped a limit of this route in this window: refused before any read
            raise _Early(_rate_limited(wait))
        try:
            principal = await external_tasks.resolve_key(key)
        except ExternalTaskRefused as refused:
            if refused.reason is TaskRefusal.KEY_INVALID:
                raise _Early(await unauthorized())
            raise
        await gate.check(principal)
        worker = f"{principal.user_id}:{principal.worker_id}"
        for scope, limit in scopes:
            wait = await limiter.hit(scope, worker, limit, digest)
            if wait is not None:
                logger.warning(f"[TaskAPI] rate limited: {scope} worker={fingerprint(principal.worker_id)}")
                raise _Early(_rate_limited(wait))
        return principal

    async def body() -> Dict[str, Any]:
        """The JSON object of a POST: content type, size cap and shape checked before any field is read."""
        if request.content_length is not None and request.content_length > BODY_CAP_BYTES:
            raise _Early(_code(413, TaskRefusal.TOO_LARGE.value))
        raw = bytearray()
        async for chunk in request.body:
            raw += chunk
            if len(raw) > BODY_CAP_BYTES:
                raise _Early(_code(413, TaskRefusal.TOO_LARGE.value))
        if raw and request.mimetype != "application/json":
            raise _Early(_code(415, "unsupported_media_type"))
        try:
            data = json.loads(bytes(raw)) if raw else {}
        except ValueError:     # invalid JSON or invalid UTF-8
            raise _Early(_code(400, "bad_request")) from None
        if not isinstance(data, dict):
            raise _Early(_code(400, "bad_request"))
        return data

    def text_field(data: Dict[str, Any], name: str) -> str:
        value = data.get(name)
        if not isinstance(value, str) or not value:
            raise _Early(_code(400, "bad_request"))
        return value

    async def serve(op: str, scopes: Tuple[Tuple[str, int], ...],
                    run: Callable[[WorkerPrincipal], Awaitable[Response]]) -> Response:
        principal: Optional[WorkerPrincipal] = None
        try:
            principal = await authenticate(scopes)
            response = await run(principal)
        except _Early as early:
            response = early.response
        except ExternalTaskRefused as refused:
            response = _code(status_for(refused.reason), refused.reason.value)
        except Exception as exc:
            who = f" worker={fingerprint(principal.worker_id)}" if principal else ""
            logger.error(f"[TaskAPI] {op} failed{who}: {type(exc).__name__}")
            raise
        if principal is not None:
            logger.info(f"[TaskAPI] {op} user={principal.user_id[:8]} worker={fingerprint(principal.worker_id)} "
                        f"status={response.status_code}")
        return response

    CALLS = (("calls", CALLS_PER_WINDOW),)

    @bp.route("/tasks", methods=["GET"])
    async def list_tasks() -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            tasks = await external_tasks.list_for_worker(principal)
            return _json(200, {"code": "ok", "tasks": [
                {"task_id": t.task_id, "status": t.status.value, "title": t.title,
                 "created_at": t.created_at.isoformat(),
                 "lease_until": t.lease_until.isoformat() if t.lease_until else None} for t in tasks]})
        return await serve("list", CALLS, run)

    @bp.route("/claim", methods=["POST"])
    async def claim_task() -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            await body()
            claimed = await external_tasks.claim(principal)
            if claimed is None:
                return Response(status=204, headers=dict(_HEADERS))
            return _json(200, {"code": "ok", "task_id": claimed.task_id, "created_at": claimed.created_at.isoformat(),
                               "lease_id": claimed.lease_id, "lease_until": claimed.lease_until.isoformat(),
                               "origin": WRITTEN_BY, "title": claimed.title, "text": claimed.text})
        return await serve("claim", CALLS + (("claims", CLAIMS_PER_WINDOW),), run)

    @bp.route("/tasks/<task_id>/renew", methods=["POST"])
    async def renew_lease(task_id: str) -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            lease_id = text_field(await body(), "lease_id")
            grant = await external_tasks.renew(principal, task_id, lease_id)
            return _json(200, {"code": "ok", "task_id": grant.task_id, "lease_until": grant.lease_until.isoformat()})
        return await serve("renew", CALLS, run)

    @bp.route("/tasks/<task_id>/result", methods=["POST"])
    async def submit_result(task_id: str) -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            data = await body()
            lease_id, result = text_field(data, "lease_id"), data.get("result")
            if not isinstance(result, str):
                raise _Early(_code(400, "bad_request"))
            receipt = await external_tasks.submit(principal, task_id, lease_id, result)
            # No `replay` flag: the same body again is the same answer.
            return _json(200, {"code": "ok", "task_id": receipt.task_id, "bytes": receipt.bytes,
                               "sha256": receipt.sha256})
        return await serve("submit", CALLS + (("submits", SUBMITS_PER_WINDOW),), run)

    @bp.route("/tasks/<task_id>/release", methods=["POST"])
    async def release_task(task_id: str) -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            lease_id = text_field(await body(), "lease_id")
            await external_tasks.release(principal, task_id, lease_id)
            return _code(200, "ok")
        return await serve("release", CALLS, run)

    @bp.route("/tasks/<task_id>/fail", methods=["POST"])
    async def fail_task(task_id: str) -> Response:
        async def run(principal: WorkerPrincipal) -> Response:
            data = await body()
            lease_id = text_field(data, "lease_id")
            reason = data.get("reason")
            if reason is not None and not isinstance(reason, str):
                raise _Early(_code(400, "bad_request"))
            await external_tasks.fail(principal, task_id, lease_id, reason)
            return _code(200, "ok")
        return await serve("fail", CALLS, run)

    return bp
