"""
NotificationSLA — per-kind service-level budget for background notifications.

Maps every ``NotificationKind`` to:
  - ``timeout_ms``: the default wall-clock budget for the kind.
  - ``tier_overrides``: optional per-PerformanceTier override of the timeout,
    used by kinds whose work envelope realistically scales with the
    underlying model tier (today: ``REMINDER`` only — a small_talk reminder
    is one quick LLM call, a deep_reasoning reminder may run multi-turn
    delegations with web search and a document generation hop).

Resolution at call time (in ``UserNotificationService.notify``):

    sla = NOTIFICATION_SLA[kind]
    timeout = sla.tier_overrides.get(tier, sla.timeout_ms) if tier else sla.timeout_ms

For kinds with no overrides the per-call ``tier`` argument is ignored —
their nature is fixed (interactive UX ceiling, formatting-only delivery,
async kick-off, etc.).

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
     docs/10_rfcs/COMPLEXITY_AWARE_SLA_RFC.md  (per-tier overrides)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

from ..domain.notification_kind import NotificationKind
from ..domain.user import PerformanceTier


@dataclass(frozen=True)
class NotificationSLA:
    """Per-kind notification budget with optional per-tier overrides.

    Attributes:
        timeout_ms: Default wall-clock budget passed to
            ``AgentMessage.timeout_ms`` and consumed by
            ``BaseAgent._execute_with_timeout``. Must be below the Cloud
            Run request timeout ceiling (30 min) for any kind that runs
            inside a synchronous HTTP request handler.
        tier_overrides: Optional mapping from ``PerformanceTier`` to
            timeout (ms) that supersedes ``timeout_ms`` when the caller
            knows which tier will execute. Empty dict (the default) =
            "no overrides — every call uses ``timeout_ms``".
    """
    timeout_ms: int
    tier_overrides: Mapping[PerformanceTier, int] = field(default_factory=dict)


# Single source of truth for per-kind budgets. Every NotificationKind MUST
# have an entry — enforced by `tests/unit/infrastructure/test_notification_sla.py`.
NOTIFICATION_SLA: Mapping[NotificationKind, NotificationSLA] = {
    # 5 min — interactive UX ceiling. The user is waiting; longer is worse
    # than failure. No tier overrides — this kind's nature is fixed by the
    # human-on-the-other-end constraint, not by the model behind it.
    NotificationKind.INTERACTIVE:       NotificationSLA(timeout_ms=300_000),

    # Reminder fires range from a one-shot small_talk acknowledgement to
    # a deep_reasoning multi-turn analysis with web research + a document
    # generation hop. Per-tier overrides scale the wall-clock budget to
    # match the work envelope:
    #   ECO         — small_talk     → 3 min
    #   BALANCED    — info_search /  → 10 min (default for the kind)
    #                 simple_analytics
    #   PERFORMANCE — deep_reasoning → 25 min (matches DAILY_DIGEST)
    # The default (600_000) is used when the caller does not pass tier=,
    # which preserves backwards-compatible behaviour for any future
    # caller that hasn't migrated.
    NotificationKind.REMINDER:          NotificationSLA(
        timeout_ms=600_000,
        tier_overrides={
            PerformanceTier.ECO:          180_000,    # 3 min
            PerformanceTier.BALANCED:     600_000,    # 10 min (matches default)
            PerformanceTier.PERFORMANCE:  1_500_000,  # 25 min — Cloud Run cap −5 min
        },
    ),

    # 25 min — multi-turn analytical work over a structured payload (e.g.
    # 31-email review with per-email get_email_details + search_web +
    # create_html_page delegation). Stays well under Cloud Run's 30 min
    # request ceiling. No tier overrides — the daily review prompt is
    # tier-independent.
    NotificationKind.DAILY_DIGEST:      NotificationSLA(timeout_ms=1_500_000),

    # 2 min — formatting-only path; Smart receives a prepared payload and
    # just renders the user-facing message that accompanies the document.
    # No overrides — formatting work doesn't scale with tier.
    NotificationKind.DOCUMENT_DELIVERY: NotificationSLA(timeout_ms=120_000),

    # 5 min — kick-off path only. Long-running research runs in a Cloud
    # Run Job; this notify just delivers the prepared report back to the
    # originating channel.
    NotificationKind.DEEP_RESEARCH:     NotificationSLA(timeout_ms=300_000),
}


def resolve_timeout_ms(
    kind: NotificationKind,
    tier: Optional[PerformanceTier] = None,
) -> int:
    """Resolve effective timeout for ``(kind, tier)``.

    Returns ``sla.tier_overrides[tier]`` when both:
      - ``tier`` is provided
      - the SLA for ``kind`` has an override for that tier

    Otherwise returns ``sla.timeout_ms``. Used by
    ``UserNotificationService.notify``; exposed as a top-level function
    so that tests and future callers can audit the resolution without
    instantiating the service.
    """
    sla = NOTIFICATION_SLA[kind]
    if tier is not None and tier in sla.tier_overrides:
        return sla.tier_overrides[tier]
    return sla.timeout_ms
