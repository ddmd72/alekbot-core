"""
OpenAI Deep Research Webhook Blueprint
=======================================

Receives POST /webhooks/openai/deep-research from OpenAI when a background
Responses API job completes, fails, or is cancelled.

Delivery model:
  OpenAI POSTs an event that carries only data.id (the SDK's webhook event types define
  no metadata or output). The handler retrieves the response; the metadata (user_id,
  account_id, query, session_id, origin_platform) was set by OpenAIDeepResearchAdapter at
  submit time and comes back with it — no extra storage needed. It then claims the job and
  enqueues the worker task `deep_research_webhook_event` (named by the job id) and returns;
  WorkerHandler delivers the report or the notice.

Signature verification (Svix/OpenAI format):
  Headers: webhook-id, webhook-timestamp, webhook-signature
  Signed content: "{webhook-id}.{webhook-timestamp}.{raw_body}"
  Secret: whsec_<base64> — OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET (.env / GCP Secret Manager)
  Algorithm: HMAC-SHA256 → base64 → compare against "v1,<base64>" in webhook-signature
  Replay protection: timestamp tolerance 300 s (matches OpenAI SDK default)

  Verification mirrors openai.resources.webhooks.AsyncWebhooks.verify_signature()
  exactly — same signed_payload format, same whsec_ decoding, same header names.

Supported event types:
  response.completed / failed / incomplete / cancelled → enqueued for the worker
  (all other types)   → acknowledged, ignored
"""

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Awaitable, Callable, Dict, Optional

from quart import Blueprint, request, jsonify

from ..ports.dedup_store import DedupStore
from ..services.task_dispatch_service import TaskDispatchService
from ..utils.logger import logger

_TIMESTAMP_TOLERANCE_SECONDS = 300  # matches OpenAI SDK default
_RESPONSE_EVENTS = frozenset({
    "response.completed", "response.failed", "response.cancelled", "response.incomplete",
})


def create_deep_research_webhooks_blueprint(
    webhook_secret: Optional[str] = None,
    task_queue: Optional[TaskDispatchService] = None,
    fetch_response: Optional[Callable[[str], Awaitable[Dict[str, Any]]]] = None,
    dedup_store: Optional[DedupStore] = None,
) -> Blueprint:
    """
    Create Quart Blueprint for OpenAI Deep Research webhook delivery.

    Args:
        webhook_secret:       OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET (whsec_<base64> format).
                              None = skip verification (local dev).
        task_queue:           Enqueues the worker task that delivers the outcome.
        fetch_response:       Retrieves the full response object by id. OpenAI webhook events
                              carry only data.id (no metadata, no output), so the handler
                              fetches the rest. None = read the event body as-is.
        dedup_store:          Claims each job id before delivery, so an OpenAI redelivery is
                              skipped. None = no dedup.
    """
    blueprint = Blueprint("deep_research_webhooks", __name__)

    if not webhook_secret:
        logger.warning(
            "[DeepResearchWebhook] No webhook_secret configured — "
            "HMAC signature verification is DISABLED (dev mode)"
        )

    def _verify_signature(raw_body: bytes, headers) -> bool:
        """
        Verify OpenAI webhook signature (Svix format).

        Mirrors openai.resources.webhooks.AsyncWebhooks.verify_signature():
          signed_content = f"{webhook-id}.{webhook-timestamp}.{body}"
          secret = base64.decode(whsec_<base64>)
          expected = base64(HMAC-SHA256(secret, signed_content))
          compare against v1,<base64> values in webhook-signature header
        """
        if not webhook_secret:
            # Fail closed in deployed environments (K_SERVICE is set by Cloud Run);
            # only a local laptop run is allowed to skip signature verification.
            if os.getenv("K_SERVICE"):
                logger.error(
                    "[DeepResearchWebhook] No webhook_secret in a deployed "
                    "environment — rejecting unverifiable webhook"
                )
                return False
            return True

        sig_header    = headers.get("webhook-signature", "")
        timestamp_str = headers.get("webhook-timestamp", "")
        webhook_id    = headers.get("webhook-id", "")

        if not (sig_header and timestamp_str and webhook_id):
            logger.warning("[DeepResearchWebhook] Missing required Svix headers")
            return False

        try:
            ts = int(timestamp_str)
        except ValueError:
            logger.warning("[DeepResearchWebhook] Invalid webhook-timestamp format")
            return False

        now = int(time.time())
        if abs(now - ts) > _TIMESTAMP_TOLERANCE_SECONDS:
            logger.warning(
                "[DeepResearchWebhook] Timestamp out of tolerance: age=%ds", now - ts
            )
            return False

        # Decode whsec_<base64> secret
        secret_str = webhook_secret
        if secret_str.startswith("whsec_"):
            decoded_secret = base64.b64decode(secret_str[6:])
        else:
            decoded_secret = secret_str.encode()

        body_str = raw_body.decode("utf-8")
        signed_content = f"{webhook_id}.{timestamp_str}.{body_str}"
        expected = base64.b64encode(
            hmac.new(decoded_secret, signed_content.encode(), hashlib.sha256).digest()
        ).decode()

        # Header may contain multiple space-separated "v1,<base64>" entries
        received_sigs = []
        for part in sig_header.split():
            received_sigs.append(part[3:] if part.startswith("v1,") else part)

        return any(hmac.compare_digest(expected, sig) for sig in received_sigs)

    @blueprint.post("/webhooks/openai/deep-research")
    async def handle_openai_deep_research():
        raw_body = await request.get_data()

        if not _verify_signature(raw_body, request.headers):
            logger.warning("[DeepResearchWebhook] Signature verification failed — rejecting")
            return jsonify({"error": "invalid signature"}), 401

        try:
            data = json.loads(raw_body)
        except Exception:
            return jsonify({"error": "invalid JSON"}), 400

        event_type   = data.get("type", "")
        response_obj = data.get("data", {})

        if event_type not in _RESPONSE_EVENTS:
            # Other event families (batch, fine_tuning, ...) share this endpoint: ack, never retrieve.
            logger.debug("[DeepResearchWebhook] Ignoring unknown event type: %s", event_type)
            return jsonify({"ok": True}), 200

        # The event itself carries only data.id — metadata and output come from the response.
        if fetch_response is not None and response_obj.get("id") and not response_obj.get("metadata"):
            try:
                response_obj = {**response_obj, **(await fetch_response(response_obj["id"]))}
            except Exception as exc:
                logger.error("[DeepResearchWebhook] Response retrieval failed: %s", exc, exc_info=True)
                return jsonify({"error": "retrieval failed"}), 500  # non-2xx: OpenAI redelivers

        metadata   = response_obj.get("metadata") or {}
        user_id    = metadata.get("user_id", "")
        account_id = metadata.get("account_id", "")
        query      = metadata.get("query", "")
        session_id = metadata.get("session_id", "")
        origin_platform = metadata.get("origin_platform") or None  # absent on pre-deploy jobs
        job_id     = response_obj.get("id", "unknown")


        logger.info(
            "[DeepResearchWebhook] Received event=%s job=%s user=%s",
            event_type, job_id[:16] if job_id else "?", user_id[:8] if user_id else "?",
        )
        if not user_id:
            # Not a job this project created (or created before metadata carried the user): nothing to deliver.
            logger.warning(
                "[DeepResearchWebhook] Empty user_id — ignoring: response_obj keys=%s metadata=%s",
                list(response_obj.keys()), metadata,
            )
            return jsonify({"ok": True}), 200

        # OpenAI redelivers on timeout or non-2xx: claim the job first.
        claim_key = f"openai_deep_research:{job_id}"
        if dedup_store is not None:
            if not await dedup_store.try_mark_processed(claim_key):
                logger.info("[DeepResearchWebhook] Duplicate delivery skipped: job=%s event=%s",
                            job_id[:16], event_type)
                return jsonify({"ok": True, "duplicate": True}), 200

        # Delivery is slow; running it here let a cancelled request strand the claim. Hand it to a named worker task.
        try:
            if task_queue is None:
                raise RuntimeError("no task queue configured")
            await task_queue.enqueue_worker_task(
                task_type="deep_research_webhook_event",
                payload={
                    "event_type": event_type, "job_id": job_id, "user_id": user_id, "account_id": account_id,
                    "session_id": session_id, "origin_platform": origin_platform or "", "query": query,
                },
                dedup_key=job_id,
            )
        except BaseException as exc:  # CancelledError included: the claim must not outlive a request that died
            logger.error("[DeepResearchWebhook] Enqueue failed: job=%s error=%r", job_id[:16], exc,
                         exc_info=True)
            if dedup_store is not None:
                try:
                    await dedup_store.release(claim_key)
                except Exception as rel_exc:
                    logger.error("[DeepResearchWebhook] claim release failed: %s", rel_exc, exc_info=True)
            if not isinstance(exc, Exception):
                raise
            return jsonify({"error": "enqueue failed"}), 500
        return jsonify({"ok": True}), 200

    return blueprint
