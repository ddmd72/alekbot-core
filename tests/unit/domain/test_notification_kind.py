"""
Unit tests for ``domain.notification_kind.NotificationKind``.

Covers:
- All five enum values present
- Stable string serialization (Cloud Task payloads / log fields rely on
  the literal string values; changing them silently breaks the wire format)
- Round-trip via ``NotificationKind(value)`` for every value
- ``str`` semantics: each member IS a string equal to its `.value`

Per:
  docs/10_rfcs/NOTIFICATION_DELIVERY_REFACTOR_RFC.md § 5 / § 8.1
"""

from __future__ import annotations

from enum import Enum

import pytest

from src.domain.notification_kind import NotificationKind


_EXPECTED_VALUES = {
    "interactive",
    "reminder",
    "daily_digest",
    "document_delivery",
    "deep_research",
}


class TestEnumShape:
    """Structural contract for the enum itself."""

    def test_is_str_enum(self):
        # (str, Enum) — required so members serialize as their `.value`
        # string in JSON (Cloud Task payloads, structured logs).
        assert issubclass(NotificationKind, str)
        assert issubclass(NotificationKind, Enum)

    def test_member_count(self):
        assert len(NotificationKind) == 5

    def test_all_expected_members_present(self):
        actual = {member.value for member in NotificationKind}
        assert actual == _EXPECTED_VALUES


class TestStringValues:
    """Each member's literal string value is load-bearing — pin them."""

    def test_interactive_value(self):
        assert NotificationKind.INTERACTIVE.value == "interactive"

    def test_reminder_value(self):
        assert NotificationKind.REMINDER.value == "reminder"

    def test_daily_digest_value(self):
        assert NotificationKind.DAILY_DIGEST.value == "daily_digest"

    def test_document_delivery_value(self):
        assert NotificationKind.DOCUMENT_DELIVERY.value == "document_delivery"

    def test_deep_research_value(self):
        assert NotificationKind.DEEP_RESEARCH.value == "deep_research"


class TestStrSemantics:
    """``(str, Enum)`` members compare and serialize as plain strings."""

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_member_equals_its_value(self, kind):
        assert kind == kind.value

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_isinstance_str(self, kind):
        assert isinstance(kind, str)

    @pytest.mark.parametrize("kind", list(NotificationKind))
    def test_round_trip(self, kind):
        assert NotificationKind(kind.value) is kind


class TestErrors:
    def test_unknown_value_raises(self):
        with pytest.raises(ValueError):
            NotificationKind("not_a_real_kind")
