"""The daily review's mail window across the fall-back night (P16-C item 3, D3).

The window is `now - 24 h` but Gmail is asked day-granular (`after:YYYY/MM/DD`, `date_to=None`; the adapter formats the
UTC date of `date_from`, pinned at the wire by `tests/unit/adapters/test_gmail_review_query_wire.py`), so consecutive
reviews overlap by up to a day and never miss. On the 25-hour day the
timestamp windows leave a one-hour hole between two reviews; the day-granular query covers it. Owner question, not a
defect: the overlap is wider than 24 h by design. Gmail may interpret `after:` dates in Pacific time (unverified); if
so a Helsinki fall-back gap could be missed. That is an owner question.
"""
from datetime import datetime, timedelta
from typing import List
from zoneinfo import ZoneInfo

import pytest

from src.ports.email_provider_port import EmailProviderPort
from src.services import email_review_service
from src.services.email_review_service import EmailReviewService
from tests.support.gmail_wire import sent_list_params
from tests.support.clock_instants import UTC, ZONES, pin_datetime, transition
from tests.support.oauth_credentials_store import CredentialsStore, creds
from src.composition.connector_registry import ConnectorRegistry

_USER = "u1"
_FAR = datetime(2099, 1, 1, tzinfo=UTC)


class _RecordingProvider(EmailProviderPort):
    """Records the `date_from`/`date_to` of every list call and returns no mail."""

    def __init__(self) -> None:
        self.calls: List[dict] = []

    async def list_emails(self, credentials, date_from=None, date_to=None, page_token=None, max_results=100, query=None):
        self.calls.append({"date_from": date_from, "date_to": date_to})
        return [], None

    async def batch_get_full_content(self, credentials, email_ids, deep=False):
        return {}

    async def refresh_token(self, credentials):
        raise AssertionError("the credentials are far from expiry; no refresh is expected")


async def _review_at(monkeypatch, instant: datetime) -> dict:
    store = CredentialsStore()
    await store.repo.save(creds(_USER, "gmail", token_expiry=_FAR))
    provider = _RecordingProvider()
    service = EmailReviewService(provider, store.repo, connector=ConnectorRegistry().get("gmail"))
    pin_datetime(monkeypatch, email_review_service, instant)
    assert await service.fetch_review_payload(_USER) == []
    (call,) = provider.calls
    return {"now": instant, **call}


async def _after_day(date_from: datetime):
    """The day the real Gmail adapter puts into `after:` for this `date_from` (read off the request it sends)."""
    q = (await sent_list_params(date_from, None))["q"]
    assert q.startswith("after:") and "before:" not in q
    return datetime.strptime(q[len("after:"):], "%Y/%m/%d").date()


@pytest.mark.parametrize("zone", ZONES)
async def test_consecutive_slots_across_the_fall_back_night_leave_no_day_unqueried(monkeypatch, zone):
    tz = ZoneInfo(zone)
    fall = transition(zone, "fall", "T").utc
    # Two consecutive daily slots at 09:12 local: the day before the transition and the transition day.
    local_day2 = datetime.combine(fall.astimezone(tz).date(), datetime.min.time()) + timedelta(hours=9, minutes=12)
    second_now = local_day2.replace(tzinfo=tz).astimezone(UTC)
    first_now = (local_day2 - timedelta(days=1)).replace(tzinfo=tz).astimezone(UTC)
    assert second_now - first_now == timedelta(hours=25), "the fall-back day is 25 hours long"

    first = await _review_at(monkeypatch, first_now)
    second = await _review_at(monkeypatch, second_now)

    assert first["date_to"] is None and second["date_to"] is None
    assert first["date_from"] == first_now - timedelta(hours=24)
    assert second["date_from"] == second_now - timedelta(hours=24)
    # By timestamp the two windows leave exactly the repeated hour out ...
    assert second["date_from"] - first["now"] == timedelta(hours=1)
    # ... and the day-granular query covers it: the second `after:` day is not later than the day of the first review.
    assert await _after_day(second["date_from"]) <= first["now"].astimezone(UTC).date()
    # Both local days of the pair are inside the union of the queried days.
    assert await _after_day(first["date_from"]) <= first_now.astimezone(tz).date()
    assert await _after_day(first["date_from"]) <= await _after_day(second["date_from"])


@pytest.mark.parametrize("zone", ZONES)
async def test_consecutive_slots_across_the_spring_forward_day_overlap(monkeypatch, zone):
    tz = ZoneInfo(zone)
    spring = transition(zone, "spring", "T").utc
    local_day2 = datetime.combine(spring.astimezone(tz).date(), datetime.min.time()) + timedelta(hours=9, minutes=12)
    second_now = local_day2.replace(tzinfo=tz).astimezone(UTC)
    first_now = (local_day2 - timedelta(days=1)).replace(tzinfo=tz).astimezone(UTC)
    assert second_now - first_now == timedelta(hours=23), "the spring-forward day is 23 hours long"

    first = await _review_at(monkeypatch, first_now)
    second = await _review_at(monkeypatch, second_now)

    assert second["date_from"] == first["now"] - timedelta(hours=1), "the timestamp windows overlap by one hour"
    assert await _after_day(second["date_from"]) <= first["now"].astimezone(UTC).date()
