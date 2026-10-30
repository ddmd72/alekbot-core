"""
Indexer service routes (docs/10_rfcs/USER_DRIVE_INDEXING_RFC.md §9 "Host", §12.5, §13).

The indexer service (`indexer_main.py`) serves exactly two routes:
- `POST /worker` — Cloud Tasks delivers `drive_sync` here from the drive queue. The service runs with
  `--ingress=internal` (RFC Δ21), and the route also checks the OIDC token in-app, stricter than the
  main service's `/worker`: the audience is pinned to this service's own URL (its only caller is
  Cloud Tasks, whose default audience is the target URL), and on Cloud Run a missing
  accepted identity (`DRIVE_INVOKER_SA_EMAIL` / `SERVICE_ACCOUNT_EMAIL`) fails closed instead of opening the route. Only local dev (no `K_SERVICE`)
  bypasses. The verifier is injected (`worker_oidc_gate`), so routing is testable without a real
  Google token. Each delivery's Cloud Tasks headers are logged like main's (task name, retry and
  execution counts: `decisions/worker_task_dedup.md`).
  Every handled tick outcome answers 200, `busy` and `throttled` included: the tick schedules its
  own continuation, and a Cloud Tasks retry on top would only duplicate it. An unknown task type is
  a 400 (a bug in the sender, never retried into success), and so is a payload without `user_id` and
  `connection_id` (MULTI_DRIVE_RFC §8: a tick is per connection); an exception the tick did not handle is
  a 500, so the queue's retry config applies. Without a tick service (the first pass of the two-pass
  first deploy: `DRIVE_INDEXER_URL` not known yet) the service still starts, so the deploy step
  succeeds, and `/worker` answers 503; nothing enqueues to it until the second pass. The indexer
  deploys after the main service (cloudbuild-dev.yaml), so on that second pass a task can meet the
  503 for a minute or two; the queue retries it.
- `GET /health` — the Cloud Run probe.
Untrusted-content processing is not a route here: it lives in the separate sandbox service (RFC §12.7).
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Awaitable, Callable, Collection, Optional

from quart import Blueprint, jsonify, request

from ..domain.cloud_task_delivery import CloudTaskDelivery
from ..domain.drive_task_keys import DRIVE_SYNC_TASK, parse_nudge, payload_scope
from ..utils.logger import logger
from .worker_oidc_verifier import verify_worker_oidc

if TYPE_CHECKING:
    from ..services.drive_indexing_service import DriveIndexingService

OidcVerifier = Callable[[Optional[str]], Awaitable[bool]]


def worker_oidc_gate(accepted_emails: Collection[str], audiences: Optional[Collection[str]] = None, *,
                     on_cloud_run: bool = False) -> OidcVerifier:
    """The indexer's `/worker` gate (RFC Δ21). With accepted identities: signature, an email in the set and,
    when given, an audience in `audiences`. Without any: open for local dev, closed on Cloud Run."""
    if isinstance(accepted_emails, str):
        raise TypeError("accepted_emails must be a collection of emails, not a str")
    accepted = frozenset(accepted_emails)
    if not accepted:
        if on_cloud_run:
            logger.error("[Indexer] no accepted service-account identity is configured on Cloud Run: "
                         "/worker refuses every call")

            async def closed(authorization: Optional[str]) -> bool:
                return False
            return closed

        async def open_for_local_dev(authorization: Optional[str]) -> bool:
            return True
        return open_for_local_dev

    async def verify(authorization: Optional[str]) -> bool:
        return verify_worker_oidc(authorization, accepted, audiences)
    return verify


def indexer_audiences(indexer_url: Optional[str]) -> Optional[Collection[str]]:
    """The audiences a drive_sync token may carry: Cloud Tasks' default (the target, `{url}/worker`) and the
    bare service URL. None when the URL is not known yet (first deploy pass: the route answers 503)."""
    base = (indexer_url or "").strip().rstrip("/")
    return (base, f"{base}/worker") if base else None


def create_indexer_worker_blueprint(indexing: "Optional[DriveIndexingService]",
                                    oidc_verifier: OidcVerifier) -> Blueprint:
    bp = Blueprint("indexer_worker", __name__)

    @bp.route("/health", methods=["GET"])
    async def health():  # type: ignore[no-untyped-def]
        return jsonify({"status": "ok"}), 200

    @bp.route("/worker", methods=["POST"])
    async def worker():  # type: ignore[no-untyped-def]
        if not await oidc_verifier(request.headers.get("Authorization")):
            logger.warning("[Indexer] rejected unauthenticated /worker request")
            return jsonify({"error": "unauthorized"}), 401
        payload = await request.get_json(silent=True) or {}
        delivery = CloudTaskDelivery.from_headers(request.headers)
        if delivery is not None:
            logger.info(
                "📥 /worker delivery: task_type=%s task=%s retry_count=%s execution_count=%s",
                payload.get("task_type"), delivery.task_name, delivery.retry_count, delivery.execution_count,
            )
        task_type = payload.get("task_type")
        if task_type != DRIVE_SYNC_TASK:
            logger.warning(f"[Indexer] unknown task_type {task_type!r}")
            return jsonify({"error": f"unknown task_type: {task_type}"}), 400
        if indexing is None:
            logger.error("[Indexer] drive_sync received but the indexer is not configured (DRIVE_INDEXER_URL)")
            return jsonify({"error": "indexer not configured"}), 503
        scope = payload_scope(payload)
        if scope is None:   # MULTI_DRIVE_RFC §8: a tick is per connection; a payload without one is refused
            logger.warning("[Indexer] drive_sync without a valid user_id and connection_id: refused")
            return jsonify({"error": "missing user_id or connection_id"}), 400
        try:
            result = await indexing.tick(scope, parse_nudge(payload))
        except Exception as exc:
            logger.error(f"[Indexer] drive_sync failed for {scope.user_id[:8]} connection "
                         f"{scope.connection_id[:8]}: {exc}", exc_info=True)
            return jsonify({"error": "tick_failed"}), 500
        return jsonify({"status": result.status, **result.summary}), 200

    return bp
