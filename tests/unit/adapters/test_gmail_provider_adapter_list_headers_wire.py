"""GmailProviderAdapter.list_headers - wire test at the aiohttp boundary (tests/support/http_boundary_fake).

Pins the Gmail query (epoch bounds, the quoted key, the three exclusions), the page size, the newest-first order and
the truncation flag; and that `list_emails` still sends calendar dates.
"""
from datetime import datetime, timedelta, timezone

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from src.domain.exceptions import OAuthScopeInsufficientError
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")
EXCLUSIONS = "-category:promotions -in:spam -in:trash"


def _msg(mid: str, date: str, subject: str = "Invoice FA-0137", sender: str = "shop@example.com") -> dict:
    return {
        "id": mid,
        "snippet": f"snippet of {mid}",
        "labelIds": ["INBOX"],
        "payload": {"headers": [
            {"name": "Subject", "value": subject},
            {"name": "From", "value": sender},
            {"name": "Date", "value": date},
        ]},
    }


@pytest.fixture
def http(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages/m1", 200, _msg("m1", "Mon, 02 Mar 2026 09:00:00 +0000"))
    fake.route("GET", f"{BASE}/messages/m2", 200, _msg("m2", "Wed, 04 Mar 2026 10:30:00 +0000"))
    fake.route("GET", f"{BASE}/messages/m3", 200, _msg("m3", "Tue, 03 Mar 2026 08:00:00 +0200"))
    yield fake
    assert fake.unrouted == []


@pytest.fixture
def adapter():
    return GmailProviderAdapter("cid", "csecret")


def _listing(http, ids, next_token=None):
    body = {"messages": [{"id": i} for i in ids]}
    if next_token:
        body["nextPageToken"] = next_token
    http.route("GET", f"{BASE}/messages", 200, body)


def _list_request(http):
    (req,) = http.requests_to("GET", f"{BASE}/messages?") or [
        r for r in http.requests if r.url == f"{BASE}/messages"]
    return req


async def test_query_carries_epoch_bounds_quoted_key_and_the_exclusions(http, adapter):
    _listing(http, [])
    after = datetime(2026, 3, 1, 23, 30, tzinfo=timezone(timedelta(hours=1)))  # 22:30 UTC
    before = datetime(2026, 3, 5, 12, 0, tzinfo=timezone.utc)

    await adapter.list_headers(CREDS, after=after, before=before, key='FA-"0137', limit=20)

    req = _list_request(http)
    assert dict(req.params) == {
        "maxResults": 20,
        "q": f'after:{int(after.timestamp())} before:{int(before.timestamp())} "FA-0137" {EXCLUSIONS}',
    }
    assert req.headers["Authorization"] == "Bearer TOKEN-1"


async def test_no_bounds_and_no_key_leave_only_the_exclusions(http, adapter):
    _listing(http, [])

    await adapter.list_headers(CREDS)

    req = _list_request(http)
    assert req.params["q"] == EXCLUSIONS
    assert req.params["maxResults"] == 100


async def test_a_naive_instant_is_read_as_utc(http, adapter):
    _listing(http, [])
    await adapter.list_headers(CREDS, after=datetime(2026, 3, 1, 0, 0))
    expected = int(datetime(2026, 3, 1, tzinfo=timezone.utc).timestamp())
    assert _list_request(http).params["q"].startswith(f"after:{expected} ")


async def test_emails_come_newest_first_and_the_flag_follows_the_page_token(http, adapter):
    _listing(http, ["m1", "m2", "m3"], next_token="NEXT")

    emails, more = await adapter.list_headers(CREDS)

    assert [e.email_id for e in emails] == ["m2", "m3", "m1"]
    assert more is True
    assert emails[0].subject == "Invoice FA-0137" and emails[0].from_address == "shop@example.com"
    assert emails[0].snippet == "snippet of m2"


async def test_no_page_token_means_nothing_beyond_the_limit(http, adapter):
    _listing(http, ["m1"])
    emails, more = await adapter.list_headers(CREDS)
    assert [e.email_id for e in emails] == ["m1"] and more is False


async def test_a_message_whose_metadata_fails_is_skipped(http, adapter):
    _listing(http, ["m1", "gone"])
    http.route("GET", f"{BASE}/messages/gone", 404, {"error": {}})
    emails, _ = await adapter.list_headers(CREDS)
    assert [e.email_id for e in emails] == ["m1"]


async def test_a_listing_error_goes_through_the_gmail_status_path(http, adapter):
    http.route("GET", f"{BASE}/messages", 403,
               {"error": {"message": "Request had insufficient authentication scopes.",
                          "errors": [{"reason": "insufficientPermissions"}]}})
    with pytest.raises(OAuthScopeInsufficientError):
        await adapter.list_headers(CREDS)


async def test_list_emails_still_sends_calendar_dates(http, adapter):
    _listing(http, [])
    await adapter.list_emails(CREDS, date_from=datetime(2026, 3, 1, 22, 30), date_to=datetime(2026, 3, 5))
    assert _list_request(http).params["q"] == "after:2026/03/01 before:2026/03/05"
