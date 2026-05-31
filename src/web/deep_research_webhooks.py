"""
OpenAI Deep Research Webhook Blueprint
=======================================

Receives POST /webhooks/openai/deep-research from OpenAI when a background
Responses API job completes, fails, or is cancelled.

Delivery model:
  OpenAI POSTs to this endpoint with the job result embedded in the payload.
  Metadata (user_id, account_id, query) is embedded by OpenAIDeepResearchAdapter
  at submit time and echoed back by OpenAI in the webhook payload — no extra
  storage needed.

Signature verification (Svix/OpenAI format):
  Headers: webhook-id, webhook-timestamp, webhook-signature
  Signed content: "{webhook-id}.{webhook-timestamp}.{raw_body}"
  Secret: whsec_<base64> — OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET (.env / GCP Secret Manager)
  Algorithm: HMAC-SHA256 → base64 → compare against "v1,<base64>" in webhook-signature
  Replay protection: timestamp tolerance 300 s (matches OpenAI SDK default)

  Verification mirrors openai.resources.webhooks.AsyncWebhooks.verify_signature()
  exactly — same signed_payload format, same whsec_ decoding, same header names.

Supported event types:
  response.completed  → deliver via notification service (SmartAgent + report link)
  response.failed     → notify user of failure
  response.cancelled  → notify user of cancellation
  (all other types)   → acknowledged, ignored
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from quart import Blueprint, request, jsonify

from ..domain.notification_kind import NotificationKind
from ..services.deep_research_delivery import NotificationPort, deliver_deep_research
from ..ports.media_storage_port import MediaStoragePort
from ..ports.prompt_content_store import PromptContentStore
from ..services.task_dispatch_service import TaskDispatchService
from ..utils.logger import logger

_TIMESTAMP_TOLERANCE_SECONDS = 300  # matches OpenAI SDK default


def create_deep_research_webhooks_blueprint(
    notification_service: NotificationPort,
    webhook_secret: Optional[str] = None,
    media_storage: Optional[MediaStoragePort] = None,
    task_queue: Optional[TaskDispatchService] = None,
    prompt_content_store: Optional[PromptContentStore] = None,
) -> Blueprint:
    """
    Create Quart Blueprint for OpenAI Deep Research webhook delivery.

    Args:
        notification_service: UserNotificationService for delivering results.
        webhook_secret:       OPENAI_DEEP_RESEARCH_WEBHOOK_SECRET (whsec_<base64> format).
                              None = skip verification (local dev).
        media_storage:        Optional GCS adapter for uploading round1/round2/meta.json
                              artifacts. None = skip artifact uploads (HTML report still
                              delivered via task_queue).
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

        metadata   = response_obj.get("metadata") or {}
        user_id    = metadata.get("user_id", "")
        account_id = metadata.get("account_id", "")
        query      = metadata.get("query", "")
        session_id = metadata.get("session_id", "")
        job_id     = response_obj.get("id", "unknown")

        logger.info(
            "[DeepResearchWebhook] Received event=%s job=%s user=%s",
            event_type, job_id[:16] if job_id else "?", user_id[:8] if user_id else "?",
        )
        if not user_id:
            logger.warning(
                "[DeepResearchWebhook] Empty user_id — response_obj keys=%s metadata=%s",
                list(response_obj.keys()), metadata,
            )

        if event_type == "response.completed":
            output_text = response_obj.get("output_text") or ""
            if not output_text:
                # Fallback: extract from output array (Responses API format)
                for item in response_obj.get("output", []):
                    if item.get("type") == "message":
                        for content in item.get("content", []):
                            if content.get("type") == "output_text":
                                output_text = content.get("text", "")
                                break
                    if output_text:
                        break

            # Durable capture BEFORE delivery — research is expensive; if delivery
            # fails, the result is already persisted in BigQuery.
            if prompt_content_store is not None:
                await prompt_content_store.record_dr_result(
                    output_text=output_text,
                    query=query,
                    user_id=user_id,
                    account_id=account_id,
                    model=response_obj.get("model", ""),
                    provider="openai",
                    source="openai_webhook",
                    job_id=job_id,
                )

            # Derive invocation channel from per-channel session_id (format: "user_id:channel_id")
            # so the report delivers to the channel that started the research, not the user's
            # primary/last-active channel. Mirrors Gemini polling path.
            origin_channel_id = session_id.split(":", 1)[1] if ":" in session_id else None

            await deliver_deep_research(
                result_text=output_text,
                user_id=user_id,
                account_id=account_id,
                query=query,
                task_queue=task_queue,
                session_id=session_id,
                media_storage=media_storage,
                channel_id_override=origin_channel_id,
            )

            logger.info("[DeepResearchWebhook] Report delivered: user=%s", user_id[:8])
            return jsonify({"ok": True}), 200

        if event_type == "response.failed":
            error = (response_obj.get("error") or {}).get("message", "unknown error")
            logger.error("[DeepResearchWebhook] Job failed: job=%s error=%s", job_id[:16], error)
            await notification_service.notify(
                kind=NotificationKind.DEEP_RESEARCH,
                user_id=user_id,
                account_id=account_id,
                system_alert="Deep research did not complete — the AI provider returned an error.",
            )
            return jsonify({"ok": True}), 200

        if event_type == "response.cancelled":
            logger.info("[DeepResearchWebhook] Job cancelled: job=%s", job_id[:16])
            await notification_service.notify(
                kind=NotificationKind.DEEP_RESEARCH,
                user_id=user_id,
                account_id=account_id,
                system_alert="Deep research was cancelled before completing.",
            )
            return jsonify({"ok": True}), 200

        # Unknown event type — acknowledge to prevent OpenAI retries
        logger.debug("[DeepResearchWebhook] Ignoring unknown event type: %s", event_type)
        return jsonify({"ok": True}), 200

    return blueprint
