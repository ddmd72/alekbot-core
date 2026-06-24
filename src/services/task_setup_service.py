"""
TaskSetupService — orchestrates MS To Do integration lifecycle.
See docs/10_rfcs/TASKS_LOCAL_FIRST_RFC.md §7.3.

Responsibilities: setup, disconnect, subscription management, status, reindex.
Called by: WorkerHandler, microsoft_tasks_webhook.py, user_cabinet_app.py.
No port needed — single implementation.
"""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from ..domain.task import TaskSubscriptionConfig, TaskUserConfig
from ..ports.oauth_credentials_port import OAuthCredentialsPort
from ..ports.task_config_port import TaskConfigPort
from ..ports.task_lifecycle_port import SubscriptionNotFoundError, TaskLifecyclePort
from ..ports.task_queue import TaskQueue
from ..ports.task_search_index import TaskSearchIndex
from ..ports.tasks_provider_port import TasksProviderPort
from ..utils.logger import logger

_MS_PROVIDER = "microsoft_todo"
_SUB_RENEWAL_THRESHOLD_H = 48  # renew if expiring within 48 hours (webhook renewal)
_SUB_EXPIRING_THRESHOLD_H = 24  # renew if expiring within 24 hours (scheduler sweep)


class TaskSetupService:
    """Orchestrates MS To Do integration lifecycle."""

    def __init__(
        self,
        lifecycle: TaskLifecyclePort,
        task_config: TaskConfigPort,
        tasks_provider: TasksProviderPort,
        oauth_credentials: OAuthCredentialsPort,
        task_search_index: TaskSearchIndex,
        task_queue: TaskQueue,
        notification_url_base: str,
    ) -> None:
        self._lifecycle = lifecycle
        self._task_config = task_config
        self._tasks_provider = tasks_provider
        self._oauth_credentials = oauth_credentials
        self._search_index = task_search_index
        self._task_queue = task_queue
        self._notification_url_base = notification_url_base

    # ------------------------------------------------------------------
    # setup
    # ------------------------------------------------------------------

    async def list_microsoft_users(self) -> List[str]:
        """Return all user_ids with stored Microsoft To Do credentials."""
        return await self._oauth_credentials.list_users_by_provider(_MS_PROVIDER)

    async def setup(self, user_id: str) -> None:
        """
        Full onboarding flow:
        1. Ensure primary "Alek Bot Tasks" list exists
        2. Persist primary_list_id if first time
        3. Ensure all lists have active webhook subscriptions
        """
        list_id = await self._lifecycle.ensure_primary_list(user_id)
        list_id = await self._task_config.set_primary_list_id_if_absent(user_id, list_id)
        await self.ensure_subscriptions(user_id, _list_id=list_id)
        logger.info(f"✅ MS To Do setup complete for user {user_id[:8]}")

    # ------------------------------------------------------------------
    # ensure_subscriptions
    # ------------------------------------------------------------------

    async def ensure_subscriptions(self, user_id: str, *, _list_id: str = None) -> None:
        """
        Ensure the primary "Alek Bot Tasks" list has an active webhook subscription.
        Registers a new subscription if absent or expired. Enqueues reindex if newly subscribed.
        Idempotent — safe to call multiple times.

        _list_id: optional hint (used by setup to avoid a redundant ensure_primary_list call).
        Scope: primary list only. Multi-list support is a future upgrade path (RFC §6.2).
        """
        config = await self._task_config.get_config(user_id)
        list_id = _list_id or config.primary_list_id
        if not list_id:
            list_id = await self._lifecycle.ensure_primary_list(user_id)
            list_id = await self._task_config.set_primary_list_id_if_absent(user_id, list_id)
        if not list_id:
            logger.warning(f"⚠️ No primary_list_id for user {user_id[:8]} — skipping subscriptions")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        active = any(
            sub.list_id == list_id and sub.expires_at > now
            for sub in config.subscriptions
        )
        if active:
            logger.debug(f"📡 Subscription for primary list already active, user {user_id[:8]}")
            await self._task_config.save_config(user_id, config)
            return

        sub = await self._lifecycle.register_subscription(
            user_id, list_id, self._notification_url_base
        )
        # Keep only subscriptions for the primary list (drop stale entries for other lists)
        config.subscriptions = [s for s in config.subscriptions if s.list_id == list_id and s.expires_at > now]
        config.subscriptions.append(sub)
        await self._task_queue.enqueue_worker_task(
            "reindex_task_list",
            {"user_id": user_id, "list_id": list_id},
        )
        logger.info(f"📡 Registered subscription for primary list {list_id[:8]}, user {user_id[:8]}")
        await self._task_config.save_config(user_id, config)

    # ------------------------------------------------------------------
    # handle_subscription_renewal
    # ------------------------------------------------------------------

    async def handle_subscription_renewal(self, user_id: str, sub_id: str) -> None:
        """
        Renew a subscription triggered by a webhook lifecycle notification.
        Only renews if the subscription expires within 48 hours.
        """
        config = await self._task_config.get_config(user_id)
        sub = self._find_sub(config, sub_id)
        if sub is None:
            logger.warning(f"⚠️ Subscription {sub_id[:8]} not found for user {user_id[:8]}")
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        threshold = now + timedelta(hours=_SUB_RENEWAL_THRESHOLD_H)
        if sub.expires_at > threshold:
            logger.debug(f"⏭️ Subscription {sub_id[:8]} still fresh — no renewal needed")
            return

        try:
            updated = await self._lifecycle.renew_subscription(user_id, sub_id)
        except SubscriptionNotFoundError:
            replacement = await self._replace_orphan(user_id, sub)
            if replacement is not None:
                config.subscriptions = [
                    replacement if s.sub_id == sub_id else s for s in config.subscriptions
                ]
            else:
                config.subscriptions = [s for s in config.subscriptions if s.sub_id != sub_id]
            await self._task_config.save_config(user_id, config)
            return

        updated = self._preserve_list_id(updated, sub)
        config.subscriptions = [updated if s.sub_id == sub_id else s for s in config.subscriptions]
        await self._task_config.save_config(user_id, config)
        logger.info(f"🔄 Renewed subscription {sub_id[:8]} for user {user_id[:8]}")

    # ------------------------------------------------------------------
    # _preserve_list_id
    # ------------------------------------------------------------------

    @staticmethod
    def _preserve_list_id(
        updated: TaskSubscriptionConfig, previous: TaskSubscriptionConfig
    ) -> TaskSubscriptionConfig:
        """
        A 204 No Content renewal carries no resource body, so the adapter returns
        an empty ``list_id``. Keep the previously stored value rather than
        clobbering it with a blank — the subscription still points at the same list.
        """
        if not updated.list_id and previous.list_id:
            return replace(updated, list_id=previous.list_id)
        return updated

    # ------------------------------------------------------------------
    # renew_expiring_subscriptions
    # ------------------------------------------------------------------

    async def renew_expiring_subscriptions(self, user_id: str) -> None:
        """
        Sweep all subscriptions expiring within 24 hours and renew them.
        Called by Cloud Scheduler (daily) via WorkerHandler.
        """
        config = await self._task_config.get_config(user_id)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        threshold = now + timedelta(hours=_SUB_EXPIRING_THRESHOLD_H)
        renewed: List[TaskSubscriptionConfig] = []

        for sub in config.subscriptions:
            if sub.expires_at <= threshold:
                try:
                    updated = await self._lifecycle.renew_subscription(user_id, sub.sub_id)
                    updated = self._preserve_list_id(updated, sub)
                    renewed.append(updated)
                    logger.info(
                        f"🔄 Renewed expiring subscription {sub.sub_id[:8]} "
                        f"for user {user_id[:8]}"
                    )
                except SubscriptionNotFoundError:
                    replacement = await self._replace_orphan(user_id, sub)
                    if replacement is not None:
                        renewed.append(replacement)
                    # else: orphan dropped; next sweep will be a no-op for
                    # this list_id; ensure_subscriptions/setup will recreate.
                except Exception as e:
                    logger.error(
                        f"❌ Failed to renew subscription {sub.sub_id[:8]}: {e}", exc_info=True
                    )
                    renewed.append(sub)  # keep old — will be retried next day
            else:
                renewed.append(sub)

        config.subscriptions = renewed
        await self._task_config.save_config(user_id, config)

    # ------------------------------------------------------------------
    # disconnect
    # ------------------------------------------------------------------

    async def disconnect(self, user_id: str) -> None:
        """
        Full teardown:
        1. Delete all Graph API webhook subscriptions
        2. Revoke OAuth credentials
        3. Delete all vector search index entries
        4. Clear persisted config
        """
        config = await self._task_config.get_config(user_id)

        for sub in config.subscriptions:
            try:
                await self._lifecycle.delete_subscription(user_id, sub.sub_id)
            except Exception as e:
                logger.warning(
                    f"⚠️ Failed to delete subscription {sub.sub_id[:8]}: {e}"
                )

        await self._oauth_credentials.revoke_credentials(user_id, _MS_PROVIDER)
        await self._search_index.delete_all_for_user(user_id)
        await self._task_config.save_config(user_id, TaskUserConfig())
        logger.info(f"✅ MS To Do disconnected for user {user_id[:8]}")

    # ------------------------------------------------------------------
    # get_status
    # ------------------------------------------------------------------

    async def get_status(self, user_id: str) -> Dict[str, Any]:
        """Return connection status and active subscription info."""
        connected = await self._oauth_credentials.is_connected(user_id, _MS_PROVIDER)
        config = await self._task_config.get_config(user_id)
        return {
            "connected": connected,
            "subscriptions": [
                {"list_id": sub.list_id, "expires_at": sub.expires_at.isoformat()}
                for sub in config.subscriptions
            ],
        }

    # ------------------------------------------------------------------
    # reindex_all
    # ------------------------------------------------------------------

    async def reindex_all(self, user_id: str) -> None:
        """
        Ensure primary list subscription is healthy, then enqueue reindex_task_list for it.
        Called from Cabinet UI or WorkerHandler on manual trigger.
        """
        await self.ensure_subscriptions(user_id)
        config = await self._task_config.get_config(user_id)
        if not config.subscriptions:
            logger.warning(f"⚠️ No subscriptions for user {user_id[:8]} — skipping reindex")
            return
        for sub in config.subscriptions:
            await self._task_queue.enqueue_worker_task(
                "reindex_task_list",
                {"user_id": user_id, "list_id": sub.list_id},
            )
        logger.info(f"📬 Enqueued reindex for {len(config.subscriptions)} list(s), user {user_id[:8]}")

    # ------------------------------------------------------------------
    # enqueue_reindex_list
    # ------------------------------------------------------------------

    async def enqueue_reindex_list(self, user_id: str, list_id: str) -> None:
        """Enqueue reindex_task_list worker task. Called by webhook on list-level notifications."""
        await self._task_queue.enqueue_worker_task(
            "reindex_task_list",
            {"user_id": user_id, "list_id": list_id},
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _replace_orphan(
        self, user_id: str, orphan: TaskSubscriptionConfig
    ) -> "TaskSubscriptionConfig | None":
        """
        Register a fresh subscription for orphan.list_id and enqueue a reindex.
        Returns the new sub, or None if re-registration fails (orphan is dropped
        in either case — caller must not keep it). Used by both the daily sweep
        and the webhook-triggered renewal paths.
        """
        try:
            replacement = await self._lifecycle.register_subscription(
                user_id, orphan.list_id, self._notification_url_base
            )
        except Exception as e:
            logger.error(
                f"❌ Failed to replace orphaned subscription {orphan.sub_id[:8]} "
                f"(list {orphan.list_id[:8]}) for user {user_id[:8]}: {e}",
                exc_info=True,
            )
            return None
        await self._task_queue.enqueue_worker_task(
            "reindex_task_list",
            {"user_id": user_id, "list_id": orphan.list_id},
        )
        logger.info(
            f"🔁 Replaced orphaned subscription {orphan.sub_id[:8]} → "
            f"{replacement.sub_id[:8]} (list {orphan.list_id[:8]}) for user {user_id[:8]}"
        )
        return replacement

    @staticmethod
    def _find_sub(
        config: TaskUserConfig, sub_id: str
    ) -> "TaskSubscriptionConfig | None":
        return next((s for s in config.subscriptions if s.sub_id == sub_id), None)
