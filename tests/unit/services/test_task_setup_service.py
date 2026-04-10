"""
Unit tests for TaskSetupService.

Mock boundary: ports (TaskLifecyclePort, TaskConfigPort, TasksProviderPort,
OAuthCredentialsPort, TaskSearchIndex, TaskQueue).

Covers:
- setup: ensure_primary_list + set_primary_list_id_if_absent + ensure_subscriptions
- ensure_subscriptions: skips lists with active subs; registers + enqueues for new/expired
- handle_subscription_renewal: renews only when < 48h remaining
- renew_expiring_subscriptions: renews all subs expiring within 24h
- disconnect: deletes subs + revokes credentials + deletes index + clears config
- get_status: returns connected flag + subscription info
- reindex_all: ensure_subscriptions + enqueue per subscription
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, call

import pytest

from src.domain.task import TaskSubscriptionConfig, TaskUserConfig
from src.ports.oauth_credentials_port import OAuthCredentialsPort
from src.ports.task_config_port import TaskConfigPort
from src.ports.task_lifecycle_port import TaskLifecyclePort
from src.ports.task_queue import TaskQueue
from src.ports.task_search_index import TaskSearchIndex
from src.ports.tasks_provider_port import TasksProviderPort
from src.services.task_setup_service import TaskSetupService
from src.domain.task import TaskList

_USER_ID = "user-1"
_LIST_ID = "list-1"
_SUB_ID = "sub-1"


def _future(hours: float) -> datetime:
    """Return a naive UTC datetime <hours> from now."""
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=hours)


def _make_sub(sub_id: str = _SUB_ID, list_id: str = _LIST_ID, hours_until_expiry: float = 72.0):
    return TaskSubscriptionConfig(
        sub_id=sub_id,
        list_id=list_id,
        expires_at=_future(hours_until_expiry),
    )


def _make_service(
    config: TaskUserConfig = None,
    lists=None,
):
    config = config or TaskUserConfig()
    lists = lists or []

    lifecycle = AsyncMock(spec=TaskLifecyclePort)
    lifecycle.ensure_primary_list.return_value = _LIST_ID
    lifecycle.register_subscription.return_value = _make_sub()
    lifecycle.renew_subscription.return_value = _make_sub(hours_until_expiry=4320.0)
    lifecycle.delete_subscription.return_value = None

    task_config = AsyncMock(spec=TaskConfigPort)
    task_config.get_config.return_value = config
    task_config.save_config.return_value = None
    task_config.set_primary_list_id_if_absent.return_value = _LIST_ID

    tasks_provider = AsyncMock(spec=TasksProviderPort)
    tasks_provider.list_task_lists.return_value = lists

    oauth = AsyncMock(spec=OAuthCredentialsPort)
    oauth.is_connected.return_value = True
    oauth.revoke_credentials.return_value = None

    search_index = AsyncMock(spec=TaskSearchIndex)
    search_index.delete_all_for_user.return_value = None

    task_queue = AsyncMock(spec=TaskQueue)
    task_queue.enqueue_worker_task.return_value = "task-name"

    svc = TaskSetupService(
        lifecycle=lifecycle,
        task_config=task_config,
        tasks_provider=tasks_provider,
        oauth_credentials=oauth,
        task_search_index=search_index,
        task_queue=task_queue,
        notification_url_base="https://example.com",
    )

    return svc, lifecycle, task_config, tasks_provider, oauth, search_index, task_queue


# =============================================================================
# setup
# =============================================================================


class TestSetup:

    async def test_calls_ensure_primary_list(self):
        svc, lifecycle, task_config, tasks_provider, _, _, _ = _make_service(
            lists=[TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")]
        )

        await svc.setup(_USER_ID)

        lifecycle.ensure_primary_list.assert_called_once_with(_USER_ID)

    async def test_calls_set_primary_list_id_if_absent(self):
        svc, lifecycle, task_config, tasks_provider, _, _, _ = _make_service(
            lists=[TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")]
        )

        await svc.setup(_USER_ID)

        task_config.set_primary_list_id_if_absent.assert_called_once_with(_USER_ID, _LIST_ID)


# =============================================================================
# ensure_subscriptions
# =============================================================================


class TestEnsureSubscriptions:

    async def test_registers_for_list_with_no_sub(self):
        lst = TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")
        svc, lifecycle, _, tasks_provider, _, _, queue = _make_service(lists=[lst])

        await svc.ensure_subscriptions(_USER_ID)

        lifecycle.register_subscription.assert_called_once_with(
            _USER_ID, _LIST_ID, "https://example.com"
        )

    async def test_enqueues_reindex_for_new_sub(self):
        lst = TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")
        svc, _, _, tasks_provider, _, _, queue = _make_service(lists=[lst])

        await svc.ensure_subscriptions(_USER_ID)

        queue.enqueue_worker_task.assert_called_once_with(
            "reindex_task_list",
            {"user_id": _USER_ID, "list_id": _LIST_ID},
        )

    async def test_skips_list_with_active_sub(self):
        active_sub = _make_sub(hours_until_expiry=72.0)
        config = TaskUserConfig(subscriptions=[active_sub])
        lst = TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")
        svc, lifecycle, _, _, _, _, queue = _make_service(config=config, lists=[lst])

        await svc.ensure_subscriptions(_USER_ID)

        lifecycle.register_subscription.assert_not_called()
        queue.enqueue_worker_task.assert_not_called()

    async def test_re_registers_expired_sub(self):
        expired_sub = _make_sub(hours_until_expiry=-1.0)  # already expired
        config = TaskUserConfig(subscriptions=[expired_sub])
        lst = TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")
        svc, lifecycle, _, _, _, _, _ = _make_service(config=config, lists=[lst])

        await svc.ensure_subscriptions(_USER_ID)

        lifecycle.register_subscription.assert_called_once()

    async def test_saves_config_after_update(self):
        lst = TaskList(list_id=_LIST_ID, name="Alek Bot Tasks")
        svc, _, task_config, _, _, _, _ = _make_service(lists=[lst])

        await svc.ensure_subscriptions(_USER_ID)

        task_config.save_config.assert_called_once()


# =============================================================================
# handle_subscription_renewal
# =============================================================================


class TestHandleSubscriptionRenewal:

    async def test_renews_when_expiring_within_48h(self):
        sub = _make_sub(hours_until_expiry=10.0)  # < 48h
        config = TaskUserConfig(subscriptions=[sub])
        svc, lifecycle, task_config, _, _, _, _ = _make_service(config=config)

        await svc.handle_subscription_renewal(_USER_ID, _SUB_ID)

        lifecycle.renew_subscription.assert_called_once_with(_USER_ID, _SUB_ID)
        task_config.save_config.assert_called_once()

    async def test_does_not_renew_when_still_fresh(self):
        sub = _make_sub(hours_until_expiry=100.0)  # > 48h
        config = TaskUserConfig(subscriptions=[sub])
        svc, lifecycle, task_config, _, _, _, _ = _make_service(config=config)

        await svc.handle_subscription_renewal(_USER_ID, _SUB_ID)

        lifecycle.renew_subscription.assert_not_called()
        task_config.save_config.assert_not_called()

    async def test_no_op_when_sub_not_found(self):
        config = TaskUserConfig(subscriptions=[])
        svc, lifecycle, task_config, _, _, _, _ = _make_service(config=config)

        await svc.handle_subscription_renewal(_USER_ID, "unknown-sub")

        lifecycle.renew_subscription.assert_not_called()
        task_config.save_config.assert_not_called()


# =============================================================================
# renew_expiring_subscriptions
# =============================================================================


class TestRenewExpiringSubscriptions:

    async def test_renews_sub_expiring_within_24h(self):
        sub = _make_sub(hours_until_expiry=10.0)
        config = TaskUserConfig(subscriptions=[sub])
        svc, lifecycle, task_config, _, _, _, _ = _make_service(config=config)

        await svc.renew_expiring_subscriptions(_USER_ID)

        lifecycle.renew_subscription.assert_called_once_with(_USER_ID, _SUB_ID)
        task_config.save_config.assert_called_once()

    async def test_skips_sub_not_expiring(self):
        sub = _make_sub(hours_until_expiry=72.0)
        config = TaskUserConfig(subscriptions=[sub])
        svc, lifecycle, task_config, _, _, _, _ = _make_service(config=config)

        await svc.renew_expiring_subscriptions(_USER_ID)

        lifecycle.renew_subscription.assert_not_called()

    async def test_saves_config_even_with_no_renewals(self):
        config = TaskUserConfig(subscriptions=[])
        svc, _, task_config, _, _, _, _ = _make_service(config=config)

        await svc.renew_expiring_subscriptions(_USER_ID)

        task_config.save_config.assert_called_once()


# =============================================================================
# disconnect
# =============================================================================


class TestDisconnect:

    async def test_deletes_all_subscriptions(self):
        sub1 = _make_sub(sub_id="sub-1")
        sub2 = _make_sub(sub_id="sub-2")
        config = TaskUserConfig(subscriptions=[sub1, sub2])
        svc, lifecycle, _, _, _, _, _ = _make_service(config=config)

        await svc.disconnect(_USER_ID)

        assert lifecycle.delete_subscription.call_count == 2

    async def test_revokes_credentials(self):
        svc, _, _, _, oauth, _, _ = _make_service()

        await svc.disconnect(_USER_ID)

        oauth.revoke_credentials.assert_called_once_with(_USER_ID, "microsoft_todo")

    async def test_deletes_search_index(self):
        svc, _, _, _, _, search_index, _ = _make_service()

        await svc.disconnect(_USER_ID)

        search_index.delete_all_for_user.assert_called_once_with(_USER_ID)

    async def test_clears_config(self):
        sub = _make_sub()
        config = TaskUserConfig(primary_list_id=_LIST_ID, subscriptions=[sub])
        svc, _, task_config, _, _, _, _ = _make_service(config=config)

        await svc.disconnect(_USER_ID)

        task_config.save_config.assert_called_once()
        saved_config: TaskUserConfig = task_config.save_config.call_args.args[1]
        assert saved_config.primary_list_id is None
        assert saved_config.subscriptions == []

    async def test_continues_on_subscription_delete_failure(self):
        sub = _make_sub()
        config = TaskUserConfig(subscriptions=[sub])
        svc, lifecycle, _, _, oauth, _, _ = _make_service(config=config)
        lifecycle.delete_subscription.side_effect = Exception("webhook error")

        await svc.disconnect(_USER_ID)

        # Should still proceed to revoke credentials
        oauth.revoke_credentials.assert_called_once()


# =============================================================================
# get_status
# =============================================================================


class TestGetStatus:

    async def test_returns_connected_true(self):
        svc, _, _, _, oauth, _, _ = _make_service()
        oauth.is_connected.return_value = True

        result = await svc.get_status(_USER_ID)

        assert result["connected"] is True

    async def test_returns_connected_false_when_no_credentials(self):
        svc, _, _, _, oauth, _, _ = _make_service()
        oauth.is_connected.return_value = False

        result = await svc.get_status(_USER_ID)

        assert result["connected"] is False

    async def test_returns_subscriptions(self):
        sub = _make_sub()
        config = TaskUserConfig(subscriptions=[sub])
        svc, _, task_config, _, _, _, _ = _make_service(config=config)
        task_config.get_config.return_value = config

        result = await svc.get_status(_USER_ID)

        assert len(result["subscriptions"]) == 1
        assert result["subscriptions"][0]["list_id"] == _LIST_ID


# =============================================================================
# reindex_all
# =============================================================================


class TestReindexAll:

    async def test_enqueues_reindex_per_subscription(self):
        sub1 = _make_sub(sub_id="sub-1", list_id="list-1")
        sub2 = _make_sub(sub_id="sub-2", list_id="list-2")
        config = TaskUserConfig(subscriptions=[sub1, sub2])
        lst1 = TaskList(list_id="list-1", name="L1")
        lst2 = TaskList(list_id="list-2", name="L2")
        svc, _, task_config, _, _, _, queue = _make_service(
            config=config, lists=[lst1, lst2]
        )
        # get_config called multiple times: first for ensure_subscriptions, then for reindex_all
        task_config.get_config.return_value = config

        await svc.reindex_all(_USER_ID)

        # enqueue_worker_task should be called at least for each subscription
        assert queue.enqueue_worker_task.call_count >= 2
