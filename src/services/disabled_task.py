"""The `/worker` task of a feature this deployment has not configured: a Null Object that answers the refusal.

Composition registers it in place of a task runner whose dependencies are absent, so the handler needs no None guard.
It is never a fake success: the body and status are the refusal the task answered when its dependency was None. It
lives in `services/` so the P9 instance ledger counts it: a `DisabledTask` built in production is an unledgered class.
"""
from typing import Any, Mapping, Tuple

from ..utils.logger import logger


class DisabledTask:
    """A `WorkerTask` that logs `reason` and answers `(body, status)`."""

    def __init__(self, reason: str, status: int, body: Mapping[str, Any]) -> None:
        self._reason = reason
        self._status = status
        self._body = dict(body)

    async def __call__(self, payload: dict[str, Any], headers: dict[str, Any]) -> Tuple[dict[str, Any], int]:
        logger.warning("[Worker] %s", self._reason)
        return dict(self._body), self._status
