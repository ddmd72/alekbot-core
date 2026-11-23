"""Wiring of the worker REST endpoint's rate limits (EXTERNAL_TASK_QUEUE_RFC §5.6, §5.11).

`main.py` calls this, never the counter or limiter constructors (`test_main_construction_allowlist.py`).
"""
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..adapters.firestore_window_counter import FirestoreWindowCounter
from ..config.environment import EnvironmentConfig
from ..services.external_task_rate_limiter import ExternalTaskRateLimiter


def build_external_task_api_limiter(db_client: Any, env_config: EnvironmentConfig, *,
                                    clock: Optional[Callable[[], datetime]] = None) -> ExternalTaskRateLimiter:
    """The limiter over the Firestore counters in the TTL collection of the task dedup marks."""
    return ExternalTaskRateLimiter(FirestoreWindowCounter(db_client, env_config),
                                   clock=clock or (lambda: datetime.now(timezone.utc)))
