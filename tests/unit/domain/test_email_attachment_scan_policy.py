"""The scan reader's admission rules (email index v2, slice 5): which scans the policy lets the reader take, and the
service-side rule that a request's page allowance must cover the whole document."""
import pytest

from src.domain.document_read import reader_admits
from src.domain.email_attachment_policy import (
    EMAIL_READER_PAGES_PER_DAY_DEFAULT, SCAN_MAX_PAGES, SCAN_READ_MIN_REMAINING_S, scan_admissible,
)


def test_the_policy_constants():
    assert (SCAN_MAX_PAGES, SCAN_READ_MIN_REMAINING_S, EMAIL_READER_PAGES_PER_DAY_DEFAULT) == (10, 60.0, 40)


@pytest.mark.parametrize("pages,expected", [(0, False), (1, True), (10, True), (11, False), (-1, False)])
def test_scan_admissible(pages, expected):
    assert scan_admissible(pages) is expected


@pytest.mark.parametrize("total,allowance,expected", [
    (0, 10, False),     # an unknown page count is never read
    (3, 0, False),      # no allowance: the default of every other caller
    (3, 3, True),
    (3, 2, False),      # all or nothing
    (10, 10, True),
    (11, 10, False),
    (1, 10, True),
])
def test_reader_admits(total, allowance, expected):
    assert reader_admits(total, allowance) is expected
