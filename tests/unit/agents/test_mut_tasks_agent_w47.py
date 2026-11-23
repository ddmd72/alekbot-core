"""`_resolve_zone` falls back to UTC for every name ZoneInfo refuses (mutation survivor 4a8f2b50)."""
from datetime import timezone

import pytest

from src.agents.tasks_agent import _resolve_zone


@pytest.mark.parametrize(
    "name",
    ["Nowhere/Land", "not-a-zone", "/etc/passwd", "../etc", "a\x00b", "", None],
)
def test_unusable_zone_name_resolves_to_utc(name):
    assert _resolve_zone(name) is timezone.utc or str(_resolve_zone(name)) == "UTC"
    assert _resolve_zone(name) is not None


def test_unknown_name_is_the_utc_singleton():
    assert _resolve_zone("Nowhere/Land") is timezone.utc


def test_value_error_name_is_the_utc_singleton():
    assert _resolve_zone("/etc/passwd") is timezone.utc


def test_known_zone_is_kept():
    assert str(_resolve_zone("Europe/Paris")) == "Europe/Paris"
