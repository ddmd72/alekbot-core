"""What Cloud Tasks says about one delivery of a task, read from its request headers.

Cloud Tasks delivers at least once: the same task can arrive twice, including while the
first attempt is still running (seen 2026-10-09: one create_html_page task, two runs 208 s
apart, two reports). `task_name` identifies the task across deliveries; the counts tell a
retry after a failed attempt from a redelivery. See
docs/04_solution_strategy/decisions/worker_task_dedup.md.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional

TASK_NAME_HEADER = "X-CloudTasks-TaskName"
RETRY_COUNT_HEADER = "X-CloudTasks-TaskRetryCount"
EXECUTION_COUNT_HEADER = "X-CloudTasks-TaskExecutionCount"


@dataclass(frozen=True)
class CloudTaskDelivery:
    task_name: str
    retry_count: Optional[int]
    execution_count: Optional[int]

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Optional["CloudTaskDelivery"]:
        """None when the request did not come from Cloud Tasks (scheduler, manual curl)."""
        task_name = headers.get(TASK_NAME_HEADER) or ""
        if not task_name:
            return None
        return cls(
            task_name=task_name,
            retry_count=_int_or_none(headers.get(RETRY_COUNT_HEADER)),
            execution_count=_int_or_none(headers.get(EXECUTION_COUNT_HEADER)),
        )


def _int_or_none(value: Optional[str]) -> Optional[int]:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None
