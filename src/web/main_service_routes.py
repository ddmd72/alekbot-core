"""
Main service routes: `/health`, `/favicon.ico` and `/worker` (the factory `main()` registers on the shared app).

Served here, not inside `main()`, so each handler is testable on a bare Quart app, like the indexer's
(`indexer_worker_app.py`):
- `GET /health` — the Cloud Run probe: `{"status": "healthy", "mode": "http"}`.
- `GET /favicon.ico` — browsers and link-preview bots request it by default; serving the logo from `static_dir`
  stops the recurring 404 noise (prod log audit, P2 hygiene batch).
- `POST /worker` — Cloud Tasks and Cloud Scheduler deliver here, delegating to `WorkerHandler`. The OIDC gate is
  injected (`oidc_gate`, built by `main()` from the accepted service-account identities): no identity configured is
  closed on Cloud Run and open locally, keeping manual curl triggers working (CLAUDE.md "Manual Triggers"). It is an
  identity-only check: the audience is not pinned, because callers use inconsistent audiences (see
  `worker_oidc_verifier`). A refused call answers 401 and never reaches the handler. The body is parsed leniently (no
  or invalid JSON is `{}`), the delivery's Cloud Tasks headers are parsed into a `CloudTaskDelivery` and logged, and
  the handler runs inside a memory-telemetry scope named after the task type (`slack_event` without one). The
  handler's `(body, status)` is returned as is.
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from quart import Blueprint, jsonify, request, send_file

from ..domain.cloud_task_delivery import CloudTaskDelivery
from ..domain.memory_telemetry import SCOPE_WORKER_TASK
from ..utils.logger import logger
from ..utils.memory_scope import memory_scope

if TYPE_CHECKING:
    from ..handlers.worker_handler import WorkerHandler

OidcGate = Callable[[Optional[str]], Awaitable[bool]]


def create_main_service_blueprint(worker_handler: "WorkerHandler", oidc_gate: OidcGate, *,
                                  static_dir: str) -> Blueprint:
    bp = Blueprint("main_service", __name__)

    @bp.route("/health", methods=["GET"])
    async def health():  # type: ignore[no-untyped-def]
        return jsonify({"status": "healthy", "mode": "http"}), 200

    @bp.route("/favicon.ico", methods=["GET"])
    async def favicon():  # type: ignore[no-untyped-def]
        return await send_file(os.path.join(static_dir, "alek-logo.png"), mimetype="image/png")

    @bp.route("/worker", methods=["POST"])
    async def worker():  # type: ignore[no-untyped-def]
        if not await oidc_gate(request.headers.get("Authorization")):
            logger.warning("Rejected unauthenticated /worker request")
            return jsonify({"error": "unauthorized"}), 401
        payload = await request.get_json(silent=True) or {}
        delivery = CloudTaskDelivery.from_headers(request.headers)
        if delivery is not None:
            logger.info(
                "📥 /worker delivery: task_type=%s task=%s retry_count=%s execution_count=%s",
                payload.get("task_type"), delivery.task_name,
                delivery.retry_count, delivery.execution_count,
            )
        with memory_scope(SCOPE_WORKER_TASK, str(payload.get("task_type") or "slack_event")):
            body, status = await worker_handler.handle(
                payload, delivery=delivery, headers=dict(request.headers),
            )
            return jsonify(body), status

    return bp
