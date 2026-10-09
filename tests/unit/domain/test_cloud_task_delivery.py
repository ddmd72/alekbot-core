"""CloudTaskDelivery — Cloud Tasks request headers → value object."""
from src.domain.cloud_task_delivery import CloudTaskDelivery


def test_from_headers_reads_name_and_counts():
    d = CloudTaskDelivery.from_headers({
        "X-CloudTasks-TaskName": "0038899772573220363",
        "X-CloudTasks-TaskRetryCount": "1",
        "X-CloudTasks-TaskExecutionCount": "0",
    })
    assert d == CloudTaskDelivery(task_name="0038899772573220363", retry_count=1, execution_count=0)


def test_no_task_name_is_not_a_cloud_task():
    """Scheduler jobs and manual curl carry no task name — nothing to dedup against."""
    assert CloudTaskDelivery.from_headers({}) is None
    assert CloudTaskDelivery.from_headers({"X-CloudTasks-TaskName": ""}) is None


def test_missing_or_garbled_counts_become_none():
    d = CloudTaskDelivery.from_headers({"X-CloudTasks-TaskName": "t", "X-CloudTasks-TaskRetryCount": "x"})
    assert d.task_name == "t"
    assert d.retry_count is None
    assert d.execution_count is None
