"""GmailProviderAdapter.list_headers - a message whose metadata cannot be parsed is skipped, the others are returned."""
import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")


@pytest.fixture
def http(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    fake.route("GET", f"{BASE}/messages", 200, {"messages": [{"id": "m1"}, {"id": "bad"}]})
    fake.route("GET", f"{BASE}/messages/m1", 200, {
        "id": "m1", "snippet": "first line", "labelIds": ["INBOX"],
        "payload": {"headers": [{"name": "Subject", "value": "Invoice FA-0137"},
                                {"name": "From", "value": "shop@example.com"},
                                {"name": "Date", "value": "Wed, 04 Mar 2026 10:30:00 +0000"}]}})
    # a reply without the message id: the metadata cannot become an EmailMetadata
    fake.route("GET", f"{BASE}/messages/bad", 200, {"payload": {"headers": []}})
    yield fake
    assert fake.unrouted == []


async def test_an_unparseable_message_is_skipped_and_the_rest_returned(http):
    emails, more = await GmailProviderAdapter("cid", "csecret").list_headers(CREDS)

    assert [e.email_id for e in emails] == ["m1"]
    assert emails[0].subject == "Invoice FA-0137" and more is False
