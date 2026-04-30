"""
NotificationSLA — per-kind service-level budget for background notifications.

Maps every ``NotificationKind`` to a single tunable parameter today:
``timeout_ms`` — the wall-clock budget the agent has to complete the
notification's underlying work (delegation loop + terminal tool).

Retry policy is deliberately NOT in this table:
  - For worker-task callers (reminders, daily review): retry is queue-level
    (Cloud Tasks queue config) and orthogonal to in-process timeout.
  - For in-process callers (BaseAgent.process retry loop):
    ``AgentConfig.config_max_retries`` already governs it, and
    ``TimeoutError`` should not be retried (it indicates a structural
    budget mismatch, not a transient failure — RFC § 11 / defect #5).

Mixing retry policy here would conflate concerns and recreate exactly
the opacity the refactor is removing.

See: docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 5
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from ..domain.notification_kind import NotificationKind


@dataclass(frozen=True)
class NotificationSLA:
    """Per-kind notification budget.

    Attributes:
        timeout_ms: Wall-clock budget passed to ``AgentMessage.timeout_ms``
            and consumed by ``BaseAgent._execute_with_timeout``. Must be
            below the Cloud Run request timeout ceiling (30 min) for any
            kind that runs inside a synchronous HTTP request handler.
    """
    timeout_ms: int


# Single source of truth for per-kind budgets. Every NotificationKind MUST
# have an entry — enforced by `tests/unit/infrastructure/test_notification_sla.py`.
NOTIFICATION_SLA: Mapping[NotificationKind, NotificationSLA] = {
    # 5 min — interactive UX ceiling. The user is waiting; longer is worse
    # than failure. Matches the legacy SmartAgentConfig.timeout_ms default.
    NotificationKind.INTERACTIVE:       NotificationSLA(timeout_ms=300_000),

    # 10 min — Smart + 1–2 specialist hops + final synthesis. Reminders may
    # call search_memory / search_web before answering.
    NotificationKind.REMINDER:          NotificationSLA(timeout_ms=600_000),

    # 25 min — multi-turn analytical work over a structured payload (e.g.
    # 31-email review with per-email get_email_details + search_web +
    # create_html_page delegation). Stays well under Cloud Run's 30 min
    # request ceiling.
    NotificationKind.DAILY_DIGEST:      NotificationSLA(timeout_ms=1_500_000),

    # 2 min — formatting-only path; Smart receives a prepared payload and
    # just renders the user-facing message that accompanies the document.
    NotificationKind.DOCUMENT_DELIVERY: NotificationSLA(timeout_ms=120_000),

    # 5 min — kick-off path only. Long-running research runs in a Cloud
    # Run Job; this notify just delivers the prepared report back to the
    # originating channel.
    NotificationKind.DEEP_RESEARCH:     NotificationSLA(timeout_ms=300_000),
}
