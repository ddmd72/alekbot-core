"""
Unit tests for ``infrastructure.notification_sla``.

Covers:
- Frozen ``NotificationSLA`` dataclass invariants
- ``NOTIFICATION_SLA`` table is exhaustive over ``NotificationKind``
  (regression catches new enum values added without a corresponding
  budget — would otherwise blow up at runtime in ``notify()``)
- Each kind's timeout is asserted by literal value (regression catches
  accidental edits, e.g. dropping a zero)
- Cloud Run request ceiling: every entry stays below the 30 min HTTP
  request limit (the only kind that could exceed it would have to run
  outside ``/worker`` — explicitly excluded by the RFC)

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 5 / § 8.1
"""

from __future__ import annotations

import dataclasses

import pytest

from src.domain.notification_kind import NotificationKind
from src.infrastructure.notification_sla import (
    NOTIFICATION_SLA,
    NotificationSLA,
)


# Cloud Run request hard ceiling. Any in-process notify path must finish
# well below this — the SLA budget is a STRICT subset of the HTTP timeout.
_CLOUD_RUN_REQUEST_CEILING_MS = 30 * 60 * 1000


class TestNotificationSLAShape:
    """Frozen dataclass invariants for the SLA value object."""

    def test_is_frozen(self):
        sla = NotificationSLA(timeout_ms=300_000)
        with pytest.raises(dataclasses.FrozenInstanceError):
            sla.timeout_ms = 1  # type: ignore[misc]

    def test_is_dataclass(self):
        assert dataclasses.is_dataclass(NotificationSLA)

    def test_field_set(self):
        names = {f.name for f in dataclasses.fields(NotificationSLA)}
        assert names == {"timeout_ms"}

    def test_equality_by_value(self):
        a = NotificationSLA(timeout_ms=300_000)
        b = NotificationSLA(timeout_ms=300_000)
        c = NotificationSLA(timeout_ms=600_000)
        assert a == b
        assert a != c


class TestSLATableExhaustive:
    """Every NotificationKind MUST have an entry — caller can index without checks."""

    def test_every_kind_has_an_entry(self):
        missing = set(NotificationKind) - set(NOTIFICATION_SLA)
        assert not missing, (
            f"NotificationKind values without SLA entries: {missing}. "
            f"Add them to NOTIFICATION_SLA — notify() indexes the table "
            f"directly and a missing key raises KeyError at runtime."
        )

    def test_no_extra_keys(self):
        # Reverse direction: no SLA entry references a kind that no
        # longer exists in the enum.
        extra = set(NOTIFICATION_SLA) - set(NotificationKind)
        assert not extra, f"NOTIFICATION_SLA has entries for unknown kinds: {extra}"

    def test_every_value_is_notification_sla(self):
        for kind, sla in NOTIFICATION_SLA.items():
            assert isinstance(sla, NotificationSLA), (
                f"NOTIFICATION_SLA[{kind!r}] = {sla!r} is not a "
                f"NotificationSLA instance."
            )


class TestSLAValues:
    """Pin every kind's literal timeout — accidental edits get caught."""

    def test_interactive_timeout(self):
        assert NOTIFICATION_SLA[NotificationKind.INTERACTIVE].timeout_ms == 300_000

    def test_reminder_timeout(self):
        assert NOTIFICATION_SLA[NotificationKind.REMINDER].timeout_ms == 600_000

    def test_daily_digest_timeout(self):
        assert NOTIFICATION_SLA[NotificationKind.DAILY_DIGEST].timeout_ms == 1_500_000

    def test_document_delivery_timeout(self):
        assert NOTIFICATION_SLA[NotificationKind.DOCUMENT_DELIVERY].timeout_ms == 120_000

    def test_deep_research_timeout(self):
        assert NOTIFICATION_SLA[NotificationKind.DEEP_RESEARCH].timeout_ms == 300_000


class TestSLABounds:
    """Every kind must stay under the Cloud Run request timeout."""

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_under_cloud_run_ceiling(self, kind):
        sla = NOTIFICATION_SLA[kind]
        assert sla.timeout_ms < _CLOUD_RUN_REQUEST_CEILING_MS, (
            f"NOTIFICATION_SLA[{kind!r}].timeout_ms = {sla.timeout_ms}ms "
            f"exceeds Cloud Run request ceiling "
            f"({_CLOUD_RUN_REQUEST_CEILING_MS}ms). Long-running work must "
            f"be moved to Cloud Run Jobs (see "
            f"docs/04_solution_strategy/decisions/cloud_tasks_vs_jobs.md)."
        )

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_positive(self, kind):
        sla = NOTIFICATION_SLA[kind]
        assert sla.timeout_ms > 0, (
            f"NOTIFICATION_SLA[{kind!r}].timeout_ms must be positive."
        )
