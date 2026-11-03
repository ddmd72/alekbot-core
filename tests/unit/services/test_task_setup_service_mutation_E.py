"""A subscription that cannot be deleted at consent does not stop the re-subscribe."""
from unittest.mock import AsyncMock

from src.domain.task import TaskUserConfig
from tests.unit.services.test_task_setup_service import _LIST_ID, _USER_ID, _make_service, _make_sub


async def test_a_failing_delete_of_an_old_subscription_still_registers_the_new_one_and_reindexes(caplog):
    old = _make_sub("sub-old")
    config = TaskUserConfig(subscriptions=[old])
    svc, lifecycle, task_config, _, _, _, queue = _make_service(config=config)
    lifecycle.delete_subscription = AsyncMock(side_effect=Exception("gone at Graph"))
    new = _make_sub("sub-new")
    lifecycle.register_subscription.return_value = new

    await svc.setup(_USER_ID)

    lifecycle.register_subscription.assert_awaited_once()
    saved = task_config.save_config.await_args.args[1]
    assert [s.sub_id for s in saved.subscriptions] == ["sub-new"]
    queue.enqueue_worker_task.assert_awaited_once_with(
        "reindex_task_list", {"user_id": _USER_ID, "list_id": _LIST_ID})
    assert any("gone at Graph" in r.getMessage() for r in caplog.records)
