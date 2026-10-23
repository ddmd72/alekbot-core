"""Facts S3 · F1 (owner, 2026-10-23): the backlog cap bounds a single run. The headroom rule and the `deferred` figure's
place in the snapshot, the day row and the ops line."""
from datetime import date, datetime, timezone

import pytest

from src.domain.fact_lifecycle_day import SNAPSHOT_FIELDS, FactLifecycleDay, check_snapshot
from src.domain.fact_lifecycle_sweep import BREAKER_BACKLOG, daily_line, stale_headroom

NOW = datetime(2026, 10, 23, 7, 30, tzinfo=timezone.utc)
ZERO = dict(due=0, newly_due=0, no_decision=0, staled=0, would_stale=0, deferred=0, stale_conflicts=0, backlog=0,
            oldest_stale_days=None, expiry_tool_errors=0)


@pytest.mark.parametrize("stale_count,expected", [(0, 50), (1, 49), (45, 5), (49, 1), (50, 0), (51, 0), (10_000, 0)])
def test_the_headroom_is_the_cap_minus_the_backlog_never_below_zero(stale_count, expected):
    assert BREAKER_BACKLOG == 50
    assert stale_headroom(stale_count) == expected


def test_deferred_is_a_snapshot_field_and_a_snapshot_without_it_is_refused():
    assert "deferred" in SNAPSHOT_FIELDS
    whole = {"run_at": NOW, "due": 1, "newly_due": 1, "no_decision": 0, "staled": 0, "would_stale": 0, "deferred": 0,
             "stale_conflicts": 0, "breaker": "ok", "backlog": 0, "oldest_stale_days": None}
    check_snapshot(whole)
    with pytest.raises(ValueError, match="deferred"):
        check_snapshot({k: v for k, v in whole.items() if k != "deferred"})
    for bad in (-1, 1.5, True, "3", None):
        with pytest.raises(ValueError):
            check_snapshot({**whole, "deferred": bad})


def test_a_day_row_reads_deferred_and_an_old_document_without_it_reads_zero():
    base = {"account_id": "acc-1", "day": "2026-10-23"}
    assert FactLifecycleDay.from_document({**base, "deferred": 7}).deferred == 7
    assert FactLifecycleDay.from_document(base).deferred == 0


def test_the_ops_line_shows_deferred_next_to_staled_and_would_stale():
    row = FactLifecycleDay(account_id="acc-12345678", day=date(2026, 10, 23), run_at=NOW, due=60, newly_due=60,
                           staled=50, deferred=10, breaker="ok")
    assert daily_line(NOW, [row], dry_run=False).splitlines()[1] == (
        "acc-1234: due 60 (new 60, no decision 0) | staled 50 / would stale 0 / deferred 10 | conflicts 0"
        " | breaker ok | backlog 0 | expiry tool errors yesterday 0"
    )


def test_a_deferred_figure_alone_earns_a_line():
    row = FactLifecycleDay(account_id="acc-1", day=date(2026, 10, 23), **{**ZERO, "deferred": 1})
    assert daily_line(NOW, [row], dry_run=True) is not None
