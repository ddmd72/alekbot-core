"""GmailProviderAdapter.batch_get_full_content - an HTML body keeps its links (owner decision O-6, 2026-11-05).

Wire test at the aiohttp boundary (tests/support/http_boundary_fake): Gmail serves an invented message, the adapter's
`body_text` is what every reader of a live mail body gets (the email agent, the daily review, the indexing
pass's classification). An external link becomes "text (cleaned URL)", the drive parser's form: no `utm_*`, no fragment, 300
characters at most with "…". Invented universe: Example School, alex@example.com; links on example.org.
"""
import base64

import pytest

from src.adapters.gmail_provider_adapter import GmailProviderAdapter
from tests.support import http_boundary_fake
from tests.support.oauth_credentials_store import creds

BASE = "https://gmail.googleapis.com/gmail/v1/users/me"
MSG = "18f0a1b2c3d4e5f7"
CREDS = creds("user-1", "gmail", access_token="TOKEN-1")


def _enc(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def _html_only(html: str) -> dict:
    return {"id": MSG, "payload": {"mimeType": "text/html", "body": {"data": _enc(html)}}}


def _alternative(parts: list) -> dict:
    return {"id": MSG, "payload": {"mimeType": "multipart/alternative", "parts": [
        {"partId": str(i), "mimeType": mime, "filename": "", "body": {"data": _enc(body)}}
        for i, (mime, body) in enumerate(parts)]}}


@pytest.fixture
def http(monkeypatch):
    fake = http_boundary_fake.install(monkeypatch)
    yield fake
    assert fake.unrouted == []


async def _body(http, message: dict) -> str:
    http.route("GET", f"{BASE}/messages/{MSG}", 200, message)
    out = await GmailProviderAdapter("cid", "csecret").batch_get_full_content(CREDS, [MSG])
    return out[MSG].body_text


async def test_an_html_only_body_keeps_the_cleaned_url_of_its_link(http):
    html = ('<html><body><p>Example School parents\' meeting on March 3, 2026: '
            '<a href="https://example.org/agenda?id=7&utm_source=news&utm_medium=mail#top">see the agenda</a>.</p>'
            '</body></html>')
    text = await _body(http, _html_only(html))
    assert "see the agenda (https://example.org/agenda?id=7)" in text
    assert "utm_" not in text and "#top" not in text


async def test_the_html_part_of_an_alternative_without_plain_text_keeps_its_links(http):
    html = '<div><a href="https://example.org/offer?id=7&pwd=xyz&utm_campaign=autumn">Autumn offer</a></div>'
    text = await _body(http, _alternative([("text/html", html)]))
    assert text == "Autumn offer (https://example.org/offer?id=7&pwd=xyz)"


async def test_a_long_link_is_cut_at_300_characters_with_an_ellipsis(http):
    url = "https://example.org/" + "a" * 380
    text = await _body(http, _html_only(f'<p><a href="{url}">Library opening hours</a></p>'))
    assert text == f"Library opening hours ({url[:300]}…)"


async def test_mailto_relative_and_in_page_links_keep_their_text_only(http):
    html = ('<p><a href="mailto:alex@example.com">Write to Alex</a> | <a href="/archive/11.html">Last issue</a> | '
            '<a href="#hours">opening hours</a></p>')
    text = await _body(http, _html_only(html))
    assert text == "Write to Alex | Last issue | opening hours"


async def test_a_link_whose_text_is_its_url_is_not_repeated(http):
    html = '<p>Agenda: <a href="https://example.org/agenda?utm_source=news">https://example.org/agenda</a></p>'
    text = await _body(http, _html_only(html))
    assert text == "Agenda: https://example.org/agenda"


async def test_a_link_with_no_text_adds_nothing(http):
    html = '<p>Hello<a href="https://example.org/pixel?id=7"><img src="https://example.org/p.gif"></a> Alex</p>'
    text = await _body(http, _html_only(html))
    assert text == "Hello Alex"


async def test_the_original_html_is_returned_unchanged(http):
    html = '<p><a href="https://example.org/agenda?utm_source=news#top">see the agenda</a></p>'
    http.route("GET", f"{BASE}/messages/{MSG}", 200, _html_only(html))
    out = await GmailProviderAdapter("cid", "csecret").batch_get_full_content(CREDS, [MSG])
    assert out[MSG].body_html == html


async def test_a_plain_text_part_still_wins_over_the_html_part(http):
    plain = "Agenda: https://example.org/agenda?id=7"
    html = '<p><a href="https://example.org/agenda?id=7">see the agenda</a></p>'
    text = await _body(http, _alternative([("text/plain", plain), ("text/html", html)]))
    assert text == plain
