"""GmailProviderAdapter.batch_get_full_content - the To and Cc addresses (`EmailFullContent.recipients`), through the
aiohttp boundary fake (tests/support/http_boundary_fake). Invented cast; reserved .example addresses."""
import base64

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MSG = "msg_rome01"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")


def _message(headers: list) -> dict:
    data = base64.urlsafe_b64encode(b"Your booking RM-55120 is confirmed.").decode().rstrip("=")
    return {"id": MSG, "payload": {"mimeType": "text/plain", "headers": headers, "body": {"size": 36, "data": data}}}


@pytest.fixture
def http(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    yield fake


async def _recipients(http, headers: list):
    http.route("GET", f"{BASE}/messages/{MSG}", 200, _message(headers))
    out = await GmailProviderAdapter("cid", "csecret").batch_get_full_content(CREDS, [MSG])
    assert http.unrouted == []
    return out[MSG].recipients


async def test_to_with_two_addresses_and_cc_with_one_come_out_in_order_without_display_names(http):
    got = await _recipients(http, [
        {"name": "Subject", "value": "Prenotazione"},
        {"name": "To", "value": "Alex Doe <alex@example.com>, \"Moreira, Clara\" <clara@example.com>"},
        {"name": "Cc", "value": "Albergo Immaginario <booking@albergo.example>"},
    ])
    assert got == ("alex@example.com", "clara@example.com", "booking@albergo.example")


async def test_header_names_are_case_insensitive_and_addresses_keep_their_case(http):
    got = await _recipients(http, [{"name": "TO", "value": "Alex.Doe@Example.com"}, {"name": "cC", "value": "jamie@example.com"}])
    assert got == ("Alex.Doe@Example.com", "jamie@example.com")


async def test_no_to_and_no_cc_is_empty(http):
    assert await _recipients(http, [{"name": "Subject", "value": "Hello"}, {"name": "Bcc", "value": "hidden@example.com"}]) == ()


async def test_an_undisclosed_recipients_group_holds_no_address(http):
    assert await _recipients(http, [{"name": "To", "value": "undisclosed-recipients:;"}]) == ()
