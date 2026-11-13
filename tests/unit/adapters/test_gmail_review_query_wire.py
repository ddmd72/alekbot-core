"""Wire test (mock at the aiohttp boundary): the DEFAULT `list_emails` query, day-granular.

`date_from` becomes `after:YYYY/MM/DD` of its own (UTC) calendar fields and `date_to=None` adds no `before:`. The email
indexing service's day cursors use this path. The daily review no longer does: it opts into exact instants
(`tests/unit/adapters/test_gmail_list_emails_exact_instants_wire.py`).
"""
from datetime import datetime

import pytest

from tests.support.clock_instants import UTC
from tests.support.gmail_wire import sent_list_params


@pytest.mark.parametrize("date_from,day", [
    (datetime(2026, 10, 24, 8, 12, tzinfo=UTC), "2026/10/24"),
    (datetime(2026, 10, 24, 23, 59, tzinfo=UTC), "2026/10/24"),
    (datetime(2026, 10, 25, 0, 0, tzinfo=UTC), "2026/10/25"),
])
async def test_default_list_sends_after_of_the_utc_date_and_no_before(date_from, day):
    params = await sent_list_params(date_from, None)
    assert params["q"] == f"after:{day}"
    assert "before:" not in params["q"]


async def test_a_date_to_adds_a_before_day():
    params = await sent_list_params(datetime(2026, 10, 24, 8, 12, tzinfo=UTC), datetime(2026, 10, 26, 1, 0, tzinfo=UTC))
    assert params["q"] == "after:2026/10/24 before:2026/10/26"


async def test_without_dates_there_is_no_query():
    assert "q" not in await sent_list_params(None, None)
