"""
Microsoft Tasks Webhook Blueprint
===================================

Receives Graph API change notifications for MS To Do tasks.
Keeps the Firestore search index fresh when users edit tasks in the MS To Do app.

POST /webhook/microsoft-tasks/{user_id}
  ?validationToken=XYZ -> return XYZ as text/plain 200 (Graph one-time validation)
  Change notification:
    1. Verify clientState == MICROSOFT_TASKS_WEBHOOK_SECRET
    2. Extract sub_id, list_id, ms_task_id from notification
    3. changeType == "deleted"        -> task_indexing.deindex_task(user_id, task_id)
       changeType == "created"|"updated" -> task_indexing.index_task_by_ref(user_id, list_id, task_id)
    4. Self-healing renewal -> task_setup.handle_subscription_renewal(user_id, sub_id)
    5. Return 202 Accepted immediately

Subscription liveness (three layers):
  1. Self-healing on every webhook receipt (this file)
  2. ensure_subscriptions() called by setup/reindex_all (TaskSetupService)
  3. Cloud Scheduler daily sweep (renew_task_subscriptions via WorkerHandler)
"""

import json
import re
from typing import Optional, TYPE_CHECKING

from quart import Blueprint, Response, request, make_response

from ..utils.logger import logger

if TYPE_CHECKING:
    from ..services.task_indexing_service import TaskIndexingService
    from ..services.task_setup_service import TaskSetupService

# Extract task_id from Graph resource path: /me/todo/lists/{list_id}/tasks/{task_id}
_TASK_RE = re.compile(r"/me/todo/lists/([^/]+)/tasks/([^/]+)")


def create_microsoft_tasks_webhook_blueprint(
    task_indexing: "TaskIndexingService",
    task_setup: "TaskSetupService",
    webhook_secret: Optional[str] = None,
) -> Blueprint:
    """
    Create Quart Blueprint for Microsoft Graph change notifications.

    Args:
        task_indexing:  TaskIndexingService — index/deindex tasks.
        task_setup:     TaskSetupService — subscription renewal.
        webhook_secret: MICROSOFT_TASKS_WEBHOOK_SECRET. None = skip verification (dev mode).
    """
    blueprint = Blueprint("microsoft_tasks_webhook", __name__)

    if not webhook_secret:
        logger.warning(
            "[MSTasksWebhook] No webhook_secret configured — "
            "clientState verification is DISABLED (dev mode)"
        )

    @blueprint.post("/webhook/microsoft-tasks/<user_id>")
    async def handle_notification(user_id: str):
        # -------------------------------------------------------------------
        # Graph one-time validation challenge
        # -------------------------------------------------------------------
        validation_token = request.args.get("validationToken")
        if validation_token:
            logger.debug(f"[MSTasksWebhook] Validation challenge for user={user_id[:8]}")
            resp = await make_response(validation_token, 200)
            resp.headers["Content-Type"] = "text/plain"
            return resp

        # -------------------------------------------------------------------
        # Parse notification payload
        # -------------------------------------------------------------------
        try:
            raw = await request.get_data()
            data = json.loads(raw)
        except Exception:
            return {"error": "invalid JSON"}, 400

        notifications = data.get("value", [])
        logger.info(
            f"[MSTasksWebhook] Received {len(notifications)} notification(s) for user={user_id[:8]}"
        )

        for notification in notifications:
            sub_id = notification.get("subscriptionId", "")
            change_type = notification.get("changeType", "")
            client_state = notification.get("clientState", "")
            resource = notification.get("resource", "")

            # Verify clientState (CSRF protection for webhooks)
            if webhook_secret and client_state != webhook_secret:
                logger.warning(
                    f"[MSTasksWebhook] clientState mismatch for sub={sub_id[:8]} — ignoring"
                )
                continue

            # Extract list_id and task_id from resource path
            match = _TASK_RE.search(resource)
            if not match:
                logger.warning(f"[MSTasksWebhook] Cannot parse resource path: {resource!r}")
                continue

            list_id = match.group(1)
            task_id = match.group(2)

            logger.info(
                f"[MSTasksWebhook] changeType={change_type} "
                f"task={task_id[:8]} list={list_id[:8]} user={user_id[:8]}"
            )

            # Index update
            try:
                if change_type == "deleted":
                    await task_indexing.deindex_task(user_id, task_id)
                elif change_type in ("created", "updated"):
                    await task_indexing.index_task_by_ref(user_id, list_id, task_id)
                else:
                    logger.debug(f"[MSTasksWebhook] Ignoring changeType={change_type!r}")
            except Exception as exc:
                logger.error(
                    f"[MSTasksWebhook] Failed to process {change_type} for task={task_id[:8]}: {exc}",
                    exc_info=True,
                )

            # Self-healing subscription renewal
            if sub_id:
                try:
                    await task_setup.handle_subscription_renewal(user_id, sub_id)
                except Exception as exc:
                    logger.warning(
                        f"[MSTasksWebhook] Subscription renewal failed for sub={sub_id[:8]}: {exc}"
                    )

        # Graph requires 202 Accepted for change notifications
        return {}, 202

    return blueprint
