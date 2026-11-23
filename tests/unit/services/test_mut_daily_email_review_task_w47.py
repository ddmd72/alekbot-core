"""An unusable `slot_date` falls back to the UTC date of the run, never to an empty string."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services import daily_email_review_task as task_module
from src.services.daily_email_review_task import DailyEmailReviewTask
from tests.support.clock_instants import pin_datetime


@pytest.mark.parametrize("raw", ["not-a-date", "2026-13-45", ""])
def test_slot_date_unparseable_string_uses_utc_date(monkeypatch: pytest.MonkeyPatch, raw: str) -> None:
    pin_datetime(monkeypatch, task_module, datetime(2026, 3, 4, 23, 30, tzinfo=timezone.utc))
    assert DailyEmailReviewTask._slot_date(raw) == "2026-03-04"
