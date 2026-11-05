"""`backoff` holds at RETRY_CAP for any attempt count (2026-11-05): a mirror document parked and retried every
MIRROR_FAILED_RETRY keeps adding attempts, and `RETRY_BASE * 2 ** attempts` overflowed timedelta at 39."""
import pytest

from src.domain.drive_index_params import RETRY_BASE, RETRY_CAP
from src.domain.drive_index_planner import backoff


@pytest.mark.parametrize("attempts, expected", [
    (0, RETRY_BASE),
    (1, RETRY_BASE * 2),
    (2, RETRY_BASE * 4),
    (6, RETRY_BASE * 64),
    (7, RETRY_CAP),
    (39, RETRY_CAP),
    (10_000, RETRY_CAP),
])
def test_backoff_doubles_to_the_cap_for_any_count(attempts, expected):
    assert backoff(attempts) == expected
