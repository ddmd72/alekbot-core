"""Unit tests for ``dispatch_deadline_s``.

A worker task enqueued without an explicit Cloud Tasks ``dispatch_deadline`` gets
the platform default of 600s. Every NotificationSLA budget above that was
therefore unreachable from the day it was written — the PERFORMANCE reminder
(1500s) and DAILY_DIGEST (1500s) were truncated at 600s, and a truncated dispatch
reads as a task failure, so Cloud Tasks retried the entire run. Three such retries
opened Smart's circuit breaker on 2026-08-15.
"""
import pytest

from src.domain.notification_kind import NotificationKind
from src.domain.user import PerformanceTier
from src.infrastructure.notification_sla import (
    DISPATCH_DEADLINE_OVERHEAD_S,
    MAX_DISPATCH_DEADLINE_S,
    NOTIFICATION_SLA,
    NotificationSLA,
    dispatch_deadline_s,
)

CLOUD_TASKS_DEFAULT_S = 600


class TestDerivation:
    def test_takes_the_worst_case_across_tier_overrides(self):
        """REMINDER defaults to 600s but reaches 1500s at PERFORMANCE. The task is
        enqueued before the tier is known, so the deadline must cover the ceiling."""
        assert dispatch_deadline_s(NotificationKind.REMINDER) == 1500 + DISPATCH_DEADLINE_OVERHEAD_S

    def test_kind_without_overrides_uses_its_default(self):
        assert dispatch_deadline_s(NotificationKind.DAILY_DIGEST) == 1500 + DISPATCH_DEADLINE_OVERHEAD_S

    def test_short_kind_stays_short(self):
        assert dispatch_deadline_s(NotificationKind.DOCUMENT_DELIVERY) == 120 + DISPATCH_DEADLINE_OVERHEAD_S


class TestBounds:
    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_never_exceeds_the_cloud_tasks_maximum(self, kind):
        assert dispatch_deadline_s(kind) <= MAX_DISPATCH_DEADLINE_S

    def test_clamped_when_a_budget_would_overflow(self, monkeypatch):
        monkeypatch.setitem(
            NOTIFICATION_SLA,
            NotificationKind.REMINDER,
            NotificationSLA(timeout_ms=3_600_000),
        )

        assert dispatch_deadline_s(NotificationKind.REMINDER) == MAX_DISPATCH_DEADLINE_S

    @pytest.mark.parametrize(
        "kind",
        [NotificationKind.REMINDER, NotificationKind.DAILY_DIGEST],
    )
    def test_long_kinds_exceed_the_cloud_tasks_default(self, kind):
        """The whole point: these two are exactly the kinds the 600s default cut."""
        assert dispatch_deadline_s(kind) > CLOUD_TASKS_DEFAULT_S


class TestOverheadIsRealHeadroom:
    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_deadline_outlives_the_agent_budget(self, kind):
        """The handler must still be able to return a status after the agent's
        budget is spent, rather than being cut mid-response."""
        sla = NOTIFICATION_SLA[kind]
        ceiling_s = max((sla.timeout_ms, *sla.tier_overrides.values())) // 1000

        assert dispatch_deadline_s(kind) > ceiling_s

    def test_performance_reminder_budget_now_fits(self):
        """The 2026-08-15 regression, stated as an invariant."""
        budget_s = NOTIFICATION_SLA[NotificationKind.REMINDER].tier_overrides[
            PerformanceTier.PERFORMANCE
        ] // 1000

        assert budget_s > CLOUD_TASKS_DEFAULT_S
        assert dispatch_deadline_s(NotificationKind.REMINDER) > budget_s
