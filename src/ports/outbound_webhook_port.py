"""OutboundWebhookPort — the one way the bot sends a request to an address a user typed (EXTERNAL_TASK_QUEUE_RFC §5.8).

Implemented by GuardedWebhookAdapter (`src/adapters/outbound_webhook/`). Used by ExternalTaskService for the
content-free wake-up POST.
"""
from abc import ABC, abstractmethod
from typing import Any, Mapping, Optional

from ..domain.wake_refusal import WakeRefusal
from ..domain.wake_target import WakeTarget


class OutboundWebhookPort(ABC):
    """Holds for every implementation:
    - The guard runs on every send, whatever was checked at save time (DNS changes): the name-level rules of
      `src/domain/wake_target.py`, then every resolved address must be public; the connection goes to the vetted
      address. No redirect is followed, no proxy is taken from the environment.
    - The answer's body is read up to a small cap and discarded: nothing of it is returned, stored or logged.
    - Logs carry the host and a code or an error class, never the URL, a header or a body.
    - Never raises for a refused target or a failed send: every outcome is a `WakeRefusal` code.
    """

    @abstractmethod
    async def post(self, target: WakeTarget, body: Mapping[str, Any]) -> Optional[WakeRefusal]:
        """POST `body` as JSON to the target with its headers. None when the target answered 2xx; otherwise the
        code (final or retryable, `WakeRefusal.is_final`)."""
