"""`DriveCapabilities` gains `writable` and `feed_scope` (MULTI_DRIVE_RFC §7.1, §11.3; design §1.3)."""
import pytest

from src.domain.drive_capabilities import DriveCapabilities
from src.domain.drive_feed_scope import FeedScope


def test_defaults_are_todays_app_folder():
    caps = DriveCapabilities(max_path_chars=400)
    assert caps.writable is True and caps.feed_scope is FeedScope.AREA


def test_the_fields_are_set():
    caps = DriveCapabilities(max_path_chars=255, writable=False, feed_scope=FeedScope.ACCOUNT)
    assert (caps.max_path_chars, caps.writable, caps.feed_scope) == (255, False, FeedScope.ACCOUNT)


def test_feed_scope_values():
    assert [s.value for s in FeedScope] == ["area", "account"]
    assert FeedScope("account") is FeedScope.ACCOUNT


def test_the_path_limit_stays_positive():
    with pytest.raises(ValueError):
        DriveCapabilities(max_path_chars=0)
