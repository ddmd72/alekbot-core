"""Wire test (aiohttp boundary): `list_emails(exact_instants=True)` sends epoch seconds, the default keeps dates.

The daily review reads a rolling 24 h. Gmail reads `after:YYYY/MM/DD` as midnight Pacific time, so the review opts in to
epoch seconds; the indexing day cursors do not opt in and their query is unchanged.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

import pytest

from src.composition.connector_registry import ConnectorRegistry
from src.services import email_review_service
from src.services.email_review_service import EmailReviewService
from tests.support.clock_instants import UTC, ZONES, pin_datetime, transition
from tests.support.oauth_credentials_store import CredentialsStore
from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")
NOW = datetime(2026, 10, 24, 9, 12, tzinfo=timezone.utc)


def _install(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages", 200, {"messages": []})
    return fake


def _q(fake) -> str:
    (req,) = [r for r in fake.requests if r.url == f"{BASE}/messages"]
    return dict(req.params)["q"]


async def test_exact_instants_send_epoch_seconds_for_after_and_before(monkeypatch):
    fake = _install(monkeypatch)
    after = NOW - timedelta(hours=24)

    await GmailProviderAdapter("cid", "csecret").list_emails(CREDS, date_from=after, date_to=NOW, exact_instants=True)

    assert _q(fake) == f"after:{int(after.timestamp())} before:{int(NOW.timestamp())}"


async def test_review_window_without_date_to_has_only_an_epoch_after(monkeypatch):
    fake = _install(monkeypatch)
    after = NOW - timedelta(hours=24)

    await GmailProviderAdapter("cid", "csecret").list_emails(CREDS, date_from=after, exact_instants=True)

    assert _q(fake) == f"after:{int(after.timestamp())}"


async def test_default_keeps_calendar_dates_for_the_indexing_cursors(monkeypatch):
    fake = _install(monkeypatch)

    await GmailProviderAdapter("cid", "csecret").list_emails(
        CREDS, date_from=NOW - timedelta(hours=24), date_to=NOW, query="in:inbox")

    assert _q(fake) == "in:inbox after:2026/10/23 before:2026/10/24"


async def test_review_service_asks_for_exact_instants_over_the_last_25_hours(monkeypatch):
    provider = await _review_provider(monkeypatch, NOW)

    kwargs = provider.list_emails.await_args.kwargs
    assert kwargs["exact_instants"] is True and kwargs["date_to"] is None
    assert kwargs["date_from"] == NOW - timedelta(hours=25)


async def test_review_query_carries_the_epoch_of_now_minus_25_hours(monkeypatch):
    fake = _install(monkeypatch)
    adapter = GmailProviderAdapter("cid", "csecret")
    provider = await _review_provider(monkeypatch, NOW, list_emails=adapter.list_emails)

    assert provider is not None
    assert _q(fake) == f"after:{int((NOW - timedelta(hours=25)).timestamp())}"


@pytest.mark.parametrize("kind", ["fall", "spring"])
@pytest.mark.parametrize("zone", ZONES)
async def test_consecutive_daily_slots_leave_no_gap(monkeypatch, zone, kind):
    tz = ZoneInfo(zone)
    t = transition(zone, kind, "T").utc
    day2 = datetime.combine(t.astimezone(tz).date(), datetime.min.time()) + timedelta(hours=9, minutes=12)
    second_now = day2.replace(tzinfo=tz).astimezone(UTC)
    first_now = (day2 - timedelta(days=1)).replace(tzinfo=tz).astimezone(UTC)

    first = (await _review_provider(monkeypatch, first_now)).list_emails.await_args.kwargs["date_from"]
    second = (await _review_provider(monkeypatch, second_now)).list_emails.await_args.kwargs["date_from"]

    assert first == first_now - timedelta(hours=25)
    assert second <= first_now, "the second window starts no later than the first review: nothing falls between"


async def _review_provider(monkeypatch, instant, list_emails=None):
    store = CredentialsStore()
    await store.repo.save(creds("u1", "gmail", token_expiry=datetime(2099, 1, 1, tzinfo=timezone.utc)))
    provider = MagicMock()
    provider.list_emails = AsyncMock(return_value=([], None))
    if list_emails is not None:
        async def run(**kw):
            return await list_emails(**kw)
        provider.list_emails = AsyncMock(side_effect=run)
    service = EmailReviewService(provider, store.repo, connector=ConnectorRegistry().get("gmail"))
    pin_datetime(monkeypatch, email_review_service, instant)
    assert await service.fetch_review_payload("u1") == []
    return provider
