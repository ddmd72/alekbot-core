"""
NotifyResult — outcome of a ``UserNotificationService.notify()`` call.

Replaces the previous silent-return pattern: ``notify()`` now reports
whether the notification was actually delivered, what status the agent
returned, and the error string when applicable. Callers (workers, cron,
webhooks) use this to decide whether to retry, mark idempotency state,
or escalate.

Frozen value object — no behavior, no I/O, pure data.

See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 6
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from .agent import AgentStatus


@dataclass(frozen=True)
class NotifyResult:
    """Result of a notify() call.

    Attributes:
        delivered: True iff the user actually received a message
            (chat-message text and/or rich content) on their channel.
            False on:
              - no channel resolved (no primary, no last-active)
              - channel factory returned None (unknown platform)
              - agent returned non-SUCCESS status
              - exception during route_message or channel send
        agent_status: status reported by the agent (or SUCCESS by default
            if the failure happened before reaching the agent — e.g. no
            channel resolved). Inspect together with ``delivered``.
        error: human-readable error string, populated when
            ``delivered=False`` due to an exception. ``None`` for
            success or for non-exception failures (e.g. agent reported
            FAILED status).
        sent: True once ``notify()`` reached its first channel send,
            set just before that call: a send that raised still counts,
            because the message may have left. A caller that marks
            "user told" before notifying rolls that back only when
            neither ``delivered`` nor ``sent`` is true.
    """
    delivered: bool
    agent_status: AgentStatus
    error: Optional[str] = None
    sent: bool = False
